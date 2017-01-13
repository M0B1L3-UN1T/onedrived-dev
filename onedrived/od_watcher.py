import logging
import os
import threading

import onedrivesdk.error
from bidict import loosebidict
from inotify_simple import flags as _inotify_flags, masks as _inotify_masks, INotify as _INotify

from . import tasks
from .models.path_filter import PathFilter
from .od_api_helper import item_request_call
from .od_hashutils import hash_match
from .od_repo import ItemRecordType
from .od_stringutils import get_filename_with_incremented_count


class LocalRepositoryWatcher:

    FLAGS = _inotify_flags.CREATE | _inotify_flags.CLOSE_WRITE | _inotify_flags.DELETE | \
            _inotify_flags.DELETE_SELF | _inotify_flags.MOVE_SELF | _inotify_masks.MOVE

    BUSY_RETRY_INTERVAL_SEC = 30
    FD_READ_DELAY_MSEC = 200

    def __init__(self, task_pool, loop=None):
        """
        :param onedrived.od_task.TaskPool task_pool:
        :param asyncio.SelectorEventLoop | None loop:
        """
        # super().__init__(name='Watcher', daemon=True)
        self._lock = threading.RLock()
        self.watch_descriptors = loosebidict()
        self.local_repos = dict()
        self.task_pool = task_pool
        self.notifier = _INotify()
        if loop is None:
            import asyncio
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = loop
        self.loop.add_reader(self.notifier.fd, self.process_events)

    def close(self):
        self.local_repos.clear()
        self.notifier.close()

    def add_repo(self, repo):
        """
        :param onedrived.od_repo.OneDriveLocalRepository repo:
        """
        self.local_repos[repo.local_root] = repo

    def add_watch(self, local_abspath):
        logging.debug('Adding watcher for "%s"', local_abspath)
        with self._lock:
            if local_abspath not in self.watch_descriptors:
                wd = self.notifier.add_watch(local_abspath, self.FLAGS)
                self.watch_descriptors[wd] = local_abspath

    def rm_watch(self, local_abspath):
        logging.debug('Removing watcher for "%s"', local_abspath)
        with self._lock:
            if local_abspath in self.watch_descriptors:
                wd = self.watch_descriptors.inv.pop(local_abspath)
                self.notifier.rm_watch(wd)

    def _path_to_repo(self, local_abspath):
        """
        :param str local_abspath:
        :return onedrived.od_repo.OneDriveLocalRepository | None:
        """
        # TODO: Either prohibit nested repo or use a more complex algorithm.
        for k, v in self.local_repos.items():
            if local_abspath.startswith(k):
                return v
        return None

    def ensure_remote_path_is_dir(self, repo, rel_path):
        """
        Make sure the path is a folder in remote repository. If the path does not exist, create it. If the path is a
        file, rename the file and create the dir. Return False if the remote path can't be made a dir.
        :param onedrived.od_repo.OneDriveLocalRepository repo:
        :param str rel_path:
        :return True | False:
        """
        if rel_path == '':
            # Drive root is guaranteed a directory.
            return True
        item_request = repo.authenticator.client.item(drive=repo.drive.id, path=rel_path)
        parent_relpath, item_name = os.path.split(rel_path)

        try:
            item = item_request_call(repo, item_request.get)

            # Return True if the remote path exists and is a directory.
            if item.folder is not None:
                return True

            # Remote path is not a directory. Try renaming it and if renaming fails, deleting it.
            new_name = get_filename_with_incremented_count(item_name)
            logging.info('Remote item "%s" in Drive %s is not a directory. Try renaming it to "%s".',
                         rel_path, repo.drive.id, new_name)
            if not tasks.move_item.MoveItemTask(repo=repo, task_pool=self.task_pool,
                                                parent_relpath=parent_relpath, item_name=item_name,
                                                new_name=new_name, is_folder=False).handle():
                if not tasks.delete_item.DeleteRemoteItemTask(repo=repo, task_pool=self.task_pool,
                                                              parent_relpath=parent_relpath,
                                                              item_name=item_name, is_folder=False).handle():
                    logging.warning('Failed to rename or delete remote item "%s" in Drive %s.',
                                    rel_path, repo.drive.id)
                    return False
        except onedrivesdk.error.OneDriveError as e:
            if e.code != onedrivesdk.error.ErrorCode.ItemNotFound:
                return False

        if not tasks.create_folder.CreateFolderTask(repo=repo, task_pool=self.task_pool,
                                                    item_name=item_name, parent_relpath=parent_relpath,
                                                    upload_if_success=False, abort_if_local_gone=True).handle():
            logging.critical('Failed to create remote directory "%s" on Drive %s.', rel_path, repo.drive.id)
            return False
        return True

    @staticmethod
    def _get_item_request_by_relpath(repo, rel_path):
        if rel_path == '':
            return repo.authenticator.client.item(drive=repo.drive.id, id='root')
        else:
            return repo.authenticator.client.item(drive=repo.drive.id, path=rel_path)

    def _add_merge_dir_task(self, repo, rel_path):
        item_request = self._get_item_request_by_relpath(repo, rel_path)
        self.task_pool.add_task(tasks.merge_dir.MergeDirectoryTask(
            repo=repo, task_pool=self.task_pool, rel_path=rel_path, item_request=item_request))

    @staticmethod
    def _local_abspath_to_relpath(repo, local_abspath):
        return local_abspath[len(repo.local_root):]

    def _handle_move_pair(self, move_pair, to_repo, from_repo=None):
        """
        :param [[inotify_simple.Event, inotify_simple.flags], [inotify_simple.Event, inotify_simple.flags]] move_pair:
        :param onedrived.od_repo.OneDriveLocalRepository to_repo:
        :param onedrived.od_repo.OneDriveLocalRepository | None from_repo:
        """
        from_tup, to_tup = move_pair
        from_ev, from_flags = from_tup
        to_ev, to_flags = to_tup

        if from_ev.name.endswith(PathFilter.TMP_SUFFIX) and from_ev.name.startswith(PathFilter.TMP_PREFIX):
            logging.debug('Move pair %s is result of renaming temp file. No need to handle.', str(move_pair))
            return

        from_parent_dir = self.watch_descriptors[from_ev.wd]
        to_parent_dir = self.watch_descriptors[to_ev.wd]
        to_parent_relpath = self._local_abspath_to_relpath(to_repo, to_parent_dir)

        if from_repo is None:
            from_repo = self._path_to_repo(from_parent_dir)

        if not self.ensure_remote_path_is_dir(repo=to_repo, rel_path=to_parent_relpath):
            logging.critical('Failed to ensure remote item for "%s" a dir. Fallback to dir merge.', to_parent_dir)
            from_parent_relpath = self._local_abspath_to_relpath(from_repo, from_parent_dir)
            if from_parent_relpath == to_parent_relpath or from_repo is not to_repo:
                self._add_merge_dir_task(to_repo, to_parent_relpath)
                if from_repo is not to_repo:
                    self._add_merge_dir_task(from_repo, from_parent_relpath)
            else:
                if to_parent_relpath == '' or from_parent_relpath.startswith(to_parent_relpath):
                    self._add_merge_dir_task(to_repo, to_parent_relpath)
                elif from_parent_relpath == '' or to_parent_relpath.startswith(from_parent_relpath):
                    self._add_merge_dir_task(from_repo, from_parent_relpath)
                else:
                    self._add_merge_dir_task(from_repo, from_parent_relpath)
                    self._add_merge_dir_task(to_repo, to_parent_relpath)
            return

        from_parent_relpath = self._local_abspath_to_relpath(from_repo, from_parent_dir)
        from_item_record = from_repo.get_item_by_path(item_name=from_ev.name, parent_relpath=from_parent_relpath)

        if from_repo is to_repo and from_item_record and \
                (from_item_record.type == ItemRecordType.FOLDER) == (_inotify_flags.ISDIR in to_flags):
            logging.info('Use Move API to move item "%s/%s" in Drive %s to "%s/%s".',
                         from_parent_relpath, from_ev.name, from_repo.drive.id, to_parent_relpath, to_ev.name)
            self.task_pool.add_task(tasks.move_item.MoveItemTask(
                repo=to_repo, task_pool=self.task_pool, parent_relpath=from_parent_relpath, item_name=from_ev.name,
                new_parent_relpath=to_parent_relpath, new_name=to_ev.name, item_id=from_item_record.item_id,
                is_folder=_inotify_flags.ISDIR in from_flags))
            return

        self._handle_unpaired_move_from(from_ev, from_flags, from_parent_dir, from_parent_relpath,
                                        from_repo, from_item_record)

        self._handle_unpaired_move_to(to_ev, to_flags, to_repo, to_parent_dir, to_parent_relpath)

    @staticmethod
    def _get_remote_item(repo, relpath):
        item_request = repo.authenticator.client.item(drive=repo.drive.id, path=relpath)
        try:
            return item_request, item_request_call(repo, item_request.get)
        except onedrivesdk.error.OneDriveError:
            return item_request, None

    def _handle_unpaired_move_from(self, from_ev, from_flags, from_parent_dir=None, from_parent_relpath=None,
                                   from_repo=None, from_item_record=None):
        """
        :param inotify_simple.Event from_ev:
        :param [inotify_simple.flags] from_flags:
        :param str | None from_parent_dir:
        :param str | None from_parent_relpath:
        :param onedrived.od_repo.OneDriveLocalRepository | None from_repo:
        :param onedrived.od_repo.ItemRecord | None from_item_record:
        """

        if from_parent_dir is None:
            from_parent_dir = self.watch_descriptors[from_ev.wd]

        if from_repo is None:
            from_repo = self._path_to_repo(from_parent_dir)

        if from_parent_relpath is None:
            from_parent_relpath = self._local_abspath_to_relpath(from_repo, from_parent_dir)

        if from_item_record is None:
            from_item_record = from_repo.get_item_by_path(item_name=from_ev.name, parent_relpath=from_parent_relpath)

        item_relpath = from_parent_relpath + '/' + from_ev.name

        item_request, item = self._get_remote_item(from_repo, item_relpath)

        if item and from_item_record and item.id == from_item_record.item_id and item.e_tag == from_item_record.e_tag:
            logging.info('Will remove item "%s/%s" in Drive %s.', from_parent_relpath, from_ev.name, from_repo.drive.id)
            self.task_pool.add_task(tasks.delete_item.DeleteRemoteItemTask(
                repo=from_repo, task_pool=self.task_pool, parent_relpath=from_parent_relpath,
                item_name=from_ev.name,
                item_id=from_item_record.item_id, is_folder=from_item_record.type == ItemRecordType.FOLDER))
        else:
            logging.info('Uncertain status of item "%s" in Drive %s for %s. Fallback to dir merge.',
                         item_relpath, from_repo.drive.id, str(from_ev))
            self._add_merge_dir_task(from_repo, from_parent_relpath)

    def _handle_unpaired_move_to(self, to_ev, to_flags, to_repo,
                                 to_parent_dir=None, to_parent_relpath=None):

        if to_parent_dir is None:
            to_parent_dir = self.watch_descriptors[to_ev.wd]

        if to_parent_relpath is None:
            to_parent_relpath = self._local_abspath_to_relpath(to_repo, to_parent_dir)

        # Check if type of the destination path matches what inotify reported.
        item_relpath = to_parent_relpath + '/' + to_ev.name
        item_local_abspath = to_parent_dir + '/' + to_ev.name
        if not os.path.exists(item_local_abspath):
            logging.info('Local path "%s" is gone when handling %s.', item_local_abspath, str(to_ev))
            return
        if os.path.isdir(item_local_abspath) != _inotify_flags.ISDIR in to_flags:
            logging.warning('Type of local path "%s" has changed since %s was reported. Fallback to dir merge.',
                            item_local_abspath, str(to_ev))
            self._add_merge_dir_task(to_repo, item_relpath)
            return

        item_request, item = self._get_remote_item(to_repo, item_relpath)

        # A move-to item doesn't have a (reliable) local record in database.

        if item is not None:
            # Remote item exists. Solve for potential type conflict.
            item_is_folder = item.folder is not None
            item_is_file = False if item_is_folder else item.file is not None
            event_is_dir = _inotify_flags.ISDIR not in to_flags
            if (item_is_folder and not event_is_dir) or (item_is_file and event_is_dir):
                # Path is a dir remotely but a file locally, or a file remotely but a dir locally.
                # To solve the type conflict we try renaming the remote item, and if it succeeds, proceed as if
                # the remote item does not exist; otherwise fall back to dir merge.
                new_name = get_filename_with_incremented_count(item.name)
                can_upload = False
                try:
                    can_upload = tasks.move_item.MoveItemTask(
                        repo=to_repo, task_pool=self.task_pool,
                        parent_relpath=to_parent_relpath, item_name=item.name, item_id=item.id,
                        new_parent_relpath=to_parent_relpath, new_name=new_name, is_folder=item_is_folder).handle()
                except onedrivesdk.error.OneDriveError as e:
                    logging.error('API error renaming remote item "%s/%s" to "%s/%s": %s. Fallback to dir merge.',
                                  to_parent_relpath, item.name, to_parent_relpath, new_name, e)
                    can_upload = False
                finally:
                    if not can_upload:
                        self._add_merge_dir_task(to_repo, to_parent_relpath)
                        return
            elif item_is_folder and event_is_dir:
                # A dir of same name already exists remotely but we don't know if it has been synced before or
                # was created on another machine. Merge the two directories.
                self.task_pool.add_task(tasks.merge_dir.MergeDirectoryTask(
                    repo=to_repo, task_pool=self.task_pool, rel_path=item_relpath, item_request=item_request))
                return
            elif item_is_file and not event_is_dir:
                if hash_match(item_local_abspath, item) and tasks.update_mtime.UpdateTimestampTask(
                        repo=to_repo, task_pool=self.task_pool,
                        parent_relpath=to_parent_relpath, item_name=to_ev.name).handle():
                    logging.info('Local file "%s" has same data as remote counterpart. Updated timestamp and record.',
                                 item_local_abspath)
                    return
            elif not item_is_folder and not item_is_file:
                logging.warning('Remote item "%s/%s" in Drive %s is neither a file nor a directory yet local item was '
                                'created due to event %s. Fallback to dir merge.',
                                to_parent_relpath, to_ev.name, to_repo.drive.id, str(to_ev))
                self._add_merge_dir_task(to_repo, to_parent_relpath)
                return

        if _inotify_flags.ISDIR in to_flags:
            self.task_pool.add_task(tasks.create_folder.CreateFolderTask(
                repo=to_repo, task_pool=self.task_pool, item_name=to_ev.name, parent_relpath=to_parent_relpath,
                upload_if_success=True, abort_if_local_gone=True))
        else:
            to_dir_request = self._get_item_request_by_relpath(to_repo, to_parent_relpath)
            self.task_pool.add_task(tasks.upload_file.UploadFileTask(
                repo=to_repo, task_pool=self.task_pool,
                parent_dir_request=to_dir_request, parent_relpath=to_parent_relpath, item_name=to_ev.name))

    def handle_event(self, ev, flags, move_pairs):
        """
        :param inotify_simple.Event ev:
        :param [inotify_simple.flags] flags:
        :param dict[int, [inotify_simple.Event, inotify_simple.flags]] move_pairs:
        """
        parent_dir = self.watch_descriptors[ev.wd]

        if _inotify_flags.DELETE_SELF in flags or _inotify_flags.MOVE_SELF in flags:
            self.watch_descriptors.pop(ev.wd)
            logging.info('Delete watcher on path "%s".', parent_dir)
            return

        repo = self._path_to_repo(parent_dir)
        if repo is None:
            logging.warning('Repo not found for %s on path "%s". Flags={%s}.',
                            str(ev), parent_dir + '/' + ev.name, ','.join([str(f) for f in flags]))
            return

        item_name = ev.name
        item_path = parent_dir
        event_isdir = _inotify_flags.ISDIR in flags
        if len(item_name):
            item_path += '/' + item_name

        if repo.path_filter.should_ignore(item_path, is_dir=event_isdir):
            logging.warning('Ignored %s on path "%s" by path filter. Flags={%s}.',
                            str(ev), parent_dir + '/' + ev.name, ','.join([str(f) for f in flags]))
            return

        if ev.cookie in move_pairs:
            # Event is part of a move-from + move-to sequence. Handle the two events at move-to time.
            if _inotify_flags.MOVED_TO in flags:
                self._handle_move_pair(move_pairs[ev.cookie], to_repo=repo)
            return
        elif _inotify_flags.MOVED_FROM in flags:
            # A move-from event without move-to counterpart.
            logging.info('Found an unpaired move-from: %s.', ev)
            return self._handle_unpaired_move_from(ev, flags,
                                                   from_parent_dir=parent_dir, from_parent_relpath=None, from_repo=repo)
        elif _inotify_flags.MOVED_TO in flags:
            # A move-to event without move-from counterpart.
            logging.info('Found an unpaired move-to: %s.', ev)
            return self._handle_unpaired_move_to(ev, flags, repo, to_parent_dir=parent_dir)

        if event_isdir and _inotify_flags.CREATE in flags:
            # A new directory was created.
            if not self.ensure_remote_path_is_dir(repo=repo, rel_path=self._local_abspath_to_relpath(repo, item_path)):
                logging.critical('Failed to create remote directory for "%s". Fallback to dir merge.', item_path)
                self._add_merge_dir_task(repo=repo, rel_path=self._local_abspath_to_relpath(repo, parent_dir))
            else:
                self.add_watch(item_path)
            return

        if _inotify_flags.CLOSE_WRITE in flags:
            # TODO: The logic here can be made smarter.
            logging.info('Local path "%s" was updated on %s. Merge the parent directory.', item_path, str(ev))
            self._add_merge_dir_task(repo, self._local_abspath_to_relpath(repo, parent_dir))
            return

        if _inotify_flags.DELETE in flags:
            logging.info('Local path "%s" was deleted on %s.', item_path, str(ev))
            return self._handle_unpaired_move_from(ev, flags,
                                                   from_parent_dir=parent_dir, from_parent_relpath=None, from_repo=repo)

        logging.info('Unhandled inotify event %s on local path "%s". Flags: %s.',
                     str(ev), item_path, ','.join([str(f) for f in flags]))

    @staticmethod
    def _recognize_event_patterns(events):
        move_pairs = dict()
        move_pairs_tmp = dict()
        all_events = []
        for ev in events:
            # Store the event and flags for chrono order processing.
            flags = _inotify_flags.from_mask(ev.mask)
            all_events.append((ev, flags))
            # Form pairs for move events.
            if _inotify_flags.MOVED_FROM in flags:
                if ev.cookie in move_pairs_tmp:
                    move_pairs[ev.cookie] = ((ev, flags), move_pairs_tmp[ev.cookie])
                    del move_pairs_tmp[ev.cookie]
                else:
                    move_pairs_tmp[ev.cookie] = (ev, flags)
            elif _inotify_flags.MOVED_TO in flags:
                if ev.cookie in move_pairs_tmp:
                    move_pairs[ev.cookie] = (move_pairs_tmp[ev.cookie], (ev, flags))
                    del move_pairs_tmp[ev.cookie]
                else:
                    move_pairs_tmp[ev.cookie] = (ev, flags)
        return move_pairs, all_events

    def process_events(self):
        if not self._lock.acquire(blocking=False):
            logging.warning('Failed to acquire the lock. Will retry in %d sec.', self.BUSY_RETRY_INTERVAL_SEC)
            self.loop.call_later(self.BUSY_RETRY_INTERVAL_SEC, self.process_events)
            return
        events = self.notifier.read(timeout=0, read_delay=self.FD_READ_DELAY_MSEC)
        if len(events):
            move_pairs, all_events = self._recognize_event_patterns(events)
            for ev, flags in all_events:
                self.handle_event(ev, flags, move_pairs)
        self._lock.release()