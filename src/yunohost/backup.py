# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2013 YunoHost

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

""" yunohost_backup.py

    Manage backups
"""
import os
import re
import sys
import json
import errno
import time
import tarfile
import shutil
import subprocess
from glob import glob
from collections import OrderedDict

from moulinette.core import MoulinetteError
from moulinette.utils import filesystem
from moulinette.utils.log import getActionLogger

from yunohost.app import app_info, app_ssowatconf, _is_installed,
from yunohost.hook import (
    hook_info, hook_callback, hook_exec, custom_hook_folder
)
from yunohost.monitor import binary_to_human
from yunohost.tools import tools_postinstall

backup_path   = '/home/yunohost.backup'
archives_path = '%s/archives' % backup_path

logger = getActionLogger('yunohost.backup')


def backup_create(name=None, description=None, output_directory=None,
                  no_compress=False, ignore_hooks=False, hooks=[],
                  ignore_apps=False, apps=[]):
    """
    Create a backup local archive

    Keyword arguments:
        name -- Name of the backup archive
        description -- Short description of the backup
        output_directory -- Output directory for the backup
        no_compress -- Do not create an archive file
        hooks -- List of backup hooks names to execute
        ignore_hooks -- Do not execute backup hooks
        apps -- List of application names to backup
        ignore_apps -- Do not backup apps

    """
    # TODO: Add a 'clean' argument to clean output directory
    tmp_dir = None

    # Validate what to backup
    if ignore_hooks and ignore_apps:
        raise MoulinetteError(errno.EINVAL,
            m18n.n('backup_action_required'))

    # Validate and define backup name
    timestamp = int(time.time())
    if not name:
        name = time.strftime('%Y%m%d-%H%M%S')
    if name in backup_list()['archives']:
        raise MoulinetteError(errno.EINVAL,
            m18n.n('backup_archive_name_exists'))

    # Validate additional arguments
    if no_compress and not output_directory:
        raise MoulinetteError(errno.EINVAL,
            m18n.n('backup_output_directory_required'))
    if output_directory:
        output_directory = os.path.abspath(output_directory)

        # Check for forbidden folders
        if output_directory.startswith(archives_path) or \
           re.match(r'^/(|(bin|boot|dev|etc|lib|root|run|sbin|sys|usr|var)(|/.*))$',
                    output_directory):
            raise MoulinetteError(errno.EINVAL,
                m18n.n('backup_output_directory_forbidden'))

        # Create the output directory
        if not os.path.isdir(output_directory):
            logger.debug("creating output directory '%s'", output_directory)
            os.makedirs(output_directory, 0750)
        # Check that output directory is empty
        elif no_compress and os.listdir(output_directory):
            raise MoulinetteError(errno.EIO,
                m18n.n('backup_output_directory_not_empty'))

        # Define temporary directory
        if no_compress:
            tmp_dir = output_directory
    else:
        output_directory = archives_path

    # Create temporary directory
    if not tmp_dir:
        tmp_dir = "%s/tmp/%s" % (backup_path, name)
        if os.path.isdir(tmp_dir):
            logger.debug("temporary directory for backup '%s' already exists",
                tmp_dir)
            filesystem.rm(tmp_dir, recursive=True)
        filesystem.mkdir(tmp_dir, 0750, parents=True, uid='admin')

    def _clean_tmp_dir(retcode=0):
        ret = hook_callback('post_backup_create', args=[tmp_dir, retcode])
        if not ret['failed']:
            filesystem.rm(tmp_dir, True, True)
        else:
            logger.warning(m18n.n('backup_cleaning_failed'))

    # Initialize backup info
    info = {
        'description': description or '',
        'created_at': timestamp,
        'apps': {},
        'hooks': {},
    }

    # Run system hooks
    if not ignore_hooks:
        # Check hooks availibility
        hooks_filtered = set()
        if hooks:
            for hook in hooks:
                try:
                    hook_info('backup', hook)
                except:
                    logger.error(m18n.n('backup_hook_unknown', hook=hook))
                else:
                    hooks_filtered.add(hook)

        if not hooks or hooks_filtered:
            logger.info(m18n.n('backup_running_hooks'))
            ret = hook_callback('backup', hooks_filtered, args=[tmp_dir])
            if ret['succeed']:
                info['hooks'] = ret['succeed']

                # Save relevant restoration hooks
                tmp_hooks_dir = tmp_dir + '/hooks/restore'
                filesystem.mkdir(tmp_hooks_dir, 0750, True, uid='admin')
                for h in ret['succeed'].keys():
                    try:
                        i = hook_info('restore', h)
                    except:
                        logger.warning(m18n.n('restore_hook_unavailable',
                                hook=h), exc_info=1)
                    else:
                        for f in i['hooks']:
                            shutil.copy(f['path'], tmp_hooks_dir)

    # Backup apps
    if not ignore_apps:
        # Filter applications to backup
        apps_list = set(os.listdir('/etc/yunohost/apps'))
        apps_filtered = set()
        if apps:
            for a in apps:
                if a not in apps_list:
                    logger.warning(m18n.n('unbackup_app', app=a))
                else:
                    apps_filtered.add(a)
        else:
            apps_filtered = apps_list

        # Run apps backup scripts
        tmp_script = '/tmp/backup_' + str(timestamp)
        for app_id in apps_filtered:
            app_setting_path = '/etc/yunohost/apps/' + app_id

            # Check if the app has a backup and restore script
            app_script = app_setting_path + '/scripts/backup'
            app_restore_script = app_setting_path + '/scripts/restore'
            if not os.path.isfile(app_script):
                logger.warning(m18n.n('unbackup_app', app=app_id))
                continue
            elif not os.path.isfile(app_restore_script):
                logger.warning(m18n.n('unrestore_app', app=app_id))

            tmp_app_dir = '{:s}/apps/{:s}'.format(tmp_dir, app_id)
            tmp_app_bkp_dir = tmp_app_dir + '/backup'
            logger.info(m18n.n('backup_running_app_script', app=app_id))
            try:
                # Prepare backup directory for the app
                filesystem.mkdir(tmp_app_bkp_dir, 0750, True, uid='admin')
                shutil.copytree(app_setting_path, tmp_app_dir + '/settings')

                # Copy app backup script in a temporary folder and execute it
                subprocess.call(['install', '-Dm555', app_script, tmp_script])
                hook_exec(tmp_script, args=[tmp_app_bkp_dir, app_id],
                          raise_on_error=True, chdir=tmp_app_bkp_dir)
            except:
                logger.exception(m18n.n('backup_app_failed', app=app_id))
                # Cleaning app backup directory
                shutil.rmtree(tmp_app_dir, ignore_errors=True)
            else:
                # Add app info
                i = app_info(app_id)
                info['apps'][app_id] = {
                    'version': i['version'],
                    'name': i['name'],
                    'description': i['description'],
                }
            finally:
                filesystem.rm(tmp_script, force=True)

    # Check if something has been saved
    if not info['hooks'] and not info['apps']:
        _clean_tmp_dir(1)
        raise MoulinetteError(errno.EINVAL, m18n.n('backup_nothings_done'))

    # Calculate total size
    size = subprocess.check_output(
        ['du','-sb', tmp_dir]).split()[0].decode('utf-8')
    info['size'] = int(size)

    # Create backup info file
    with open("%s/info.json" % tmp_dir, 'w') as f:
        f.write(json.dumps(info))

    # Create the archive
    if not no_compress:
        logger.info(m18n.n('backup_creating_archive'))
        archive_file = "%s/%s.tar.gz" % (output_directory, name)
        try:
            tar = tarfile.open(archive_file, "w:gz")
        except:
            tar = None

            # Create the archives directory and retry
            if not os.path.isdir(archives_path):
                os.mkdir(archives_path, 0750)
                try:
                    tar = tarfile.open(archive_file, "w:gz")
                except:
                    logger.debug("unable to open '%s' for writing",
                        archive_file, exc_info=1)
                    tar = None
            else:
                logger.debug("unable to open '%s' for writing",
                    archive_file, exc_info=1)
            if tar is None:
                _clean_tmp_dir(2)
                raise MoulinetteError(errno.EIO,
                    m18n.n('backup_archive_open_failed'))
        tar.add(tmp_dir, arcname='')
        tar.close()

        # Move info file
        os.rename(tmp_dir + '/info.json',
                  '{:s}/{:s}.info.json'.format(archives_path, name))

    # Clean temporary directory
    if tmp_dir != output_directory:
        _clean_tmp_dir()

    logger.success(m18n.n('backup_complete'))

    # Return backup info
    info['name'] = name
    return { 'archive': info }


def backup_restore(auth, name, hooks=[], ignore_hooks=False,
                   apps=[], ignore_apps=False, force=False):
    """
    Restore from a local backup archive

    Keyword argument:
        name -- Name of the local backup archive
        hooks -- List of restoration hooks names to execute
        ignore_hooks -- Do not execute backup hooks
        apps -- List of application names to restore
        ignore_apps -- Do not restore apps
        force -- Force restauration on an already installed system

    """
    # Validate what to restore
    if ignore_hooks and ignore_apps:
        raise MoulinetteError(errno.EINVAL,
            m18n.n('restore_action_required'))

    # Retrieve and open the archive
    info = backup_info(name)
    archive_file = info['path']
    try:
        tar = tarfile.open(archive_file, "r:gz")
    except:
        logger.debug("cannot open backup archive '%s'",
            archive_file, exc_info=1)
        raise MoulinetteError(errno.EIO, m18n.n('backup_archive_open_failed'))

    # Check temporary directory
    tmp_dir = "%s/tmp/%s" % (backup_path, name)
    if os.path.isdir(tmp_dir):
        logger.debug("temporary directory for restoration '%s' already exists",
            tmp_dir)
        os.system('rm -rf %s' % tmp_dir)

    # Check available disk space
    statvfs = os.statvfs(backup_path)
    free_space = statvfs.f_frsize * statvfs.f_bavail
    if free_space < info['size']:
        logger.debug("%dB left but %dB is needed", free_space, info['size'])
        raise MoulinetteError(
            errno.EIO, m18n.n('not_enough_disk_space', path=backup_path))

    def _clean_tmp_dir(retcode=0):
        ret = hook_callback('post_backup_restore', args=[tmp_dir, retcode])
        if not ret['failed']:
            filesystem.rm(tmp_dir, True, True)
        else:
            logger.warning(m18n.n('restore_cleaning_failed'))

    # Extract the tarball
    logger.info(m18n.n('backup_extracting_archive'))
    tar.extractall(tmp_dir)
    tar.close()

    # Retrieve backup info
    info_file = "%s/info.json" % tmp_dir
    try:
        with open(info_file, 'r') as f:
            info = json.load(f)
    except IOError:
        logger.debug("unable to load '%s'", info_file, exc_info=1)
        raise MoulinetteError(errno.EIO, m18n.n('backup_invalid_archive'))
    else:
        logger.debug("restoring from backup '%s' created on %s", name,
            time.ctime(info['created_at']))

    # Initialize restauration summary result
    result = {
        'apps': [],
        'hooks': {},
    }

    # Check if YunoHost is installed
    if os.path.isfile('/etc/yunohost/installed'):
        logger.warning(m18n.n('yunohost_already_installed'))
        if not force:
            try:
                # Ask confirmation for restoring
                i = msignals.prompt(m18n.n('restore_confirm_yunohost_installed',
                                           answers='y/N'))
            except NotImplemented:
                pass
            else:
                if i == 'y' or i == 'Y':
                    force = True
            if not force:
                _clean_tmp_dir()
                raise MoulinetteError(errno.EEXIST, m18n.n('restore_failed'))
    else:
        # Retrieve the domain from the backup
        try:
            with open("%s/yunohost/current_host" % tmp_dir, 'r') as f:
                domain = f.readline().rstrip()
        except IOError:
            logger.debug("unable to retrieve domain from "
                "'%s/yunohost/current_host'", tmp_dir, exc_info=1)
            raise MoulinetteError(errno.EIO, m18n.n('backup_invalid_archive'))

        logger.debug("executing the post-install...")
        tools_postinstall(domain, 'yunohost', True)

    # Run system hooks
    if not ignore_hooks:
        # Filter hooks to execute
        hooks_list = set(info['hooks'].keys())
        _is_hook_in_backup = lambda h: True
        if hooks:
            def _is_hook_in_backup(h):
                if h in hooks_list:
                    return True
                logger.error(m18n.n('backup_archive_hook_not_exec', hook=h))
                return False
        else:
            hooks = hooks_list

        # Check hooks availibility
        hooks_filtered = set()
        for h in hooks:
            if not _is_hook_in_backup(h):
                continue
            try:
                hook_info('restore', h)
            except:
                tmp_hooks = glob('{:s}/hooks/restore/*-{:s}'.format(tmp_dir, h))
                if not tmp_hooks:
                    logger.exception(m18n.n('restore_hook_unavailable', hook=h))
                    continue
                # Add restoration hook from the backup to the system
                # FIXME: Refactor hook_add and use it instead
                restore_hook_folder = custom_hook_folder + 'restore'
                filesystem.mkdir(restore_hook_folder, 755, True)
                for f in tmp_hooks:
                    logger.debug("adding restoration hook '%s' to the system "
                        "from the backup archive '%s'", f, archive_file)
                    shutil.copy(f, restore_hook_folder)
            hooks_filtered.add(h)

        if hooks_filtered:
            logger.info(m18n.n('restore_running_hooks'))
            ret = hook_callback('restore', hooks_filtered, args=[tmp_dir])
            result['hooks'] = ret['succeed']

    # Add apps restore hook
    if not ignore_apps:
        # Filter applications to restore
        apps_list = set(info['apps'].keys())
        apps_filtered = set()
        if apps:
            for a in apps:
                if a not in apps_list:
                    logger.error(m18n.n('backup_archive_app_not_found', app=a))
                else:
                    apps_filtered.add(a)
        else:
            apps_filtered = apps_list

        for app_id in apps_filtered:
            tmp_app_dir = '{:s}/apps/{:s}'.format(tmp_dir, app_id)
            tmp_app_bkp_dir = tmp_app_dir + '/backup'

            # Check if the app is not already installed
            if _is_installed(app_id):
                logger.error(m18n.n('restore_already_installed_app',
                        app=app_id))
                continue

            # Check if the app has a restore script
            app_script = tmp_app_dir + '/settings/scripts/restore'
            if not os.path.isfile(app_script):
                logger.warning(m18n.n('unrestore_app', app=app_id))
                continue

            tmp_script = '/tmp/restore_' + app_id
            app_setting_path = '/etc/yunohost/apps/' + app_id
            logger.info(m18n.n('restore_running_app_script', app=app_id))
            try:
                # Copy app settings and set permissions
                shutil.copytree(tmp_app_dir + '/settings', app_setting_path)
                filesystem.chmod(app_setting_path, 0555, 0444, True)
                filesystem.chmod(app_setting_path + '/settings.yml', 0400)

                # Execute app restore script
                subprocess.call(['install', '-Dm555', app_script, tmp_script])
                hook_exec(tmp_script, args=[tmp_app_bkp_dir, app_id],
                          raise_on_error=True, chdir=tmp_app_bkp_dir)
            except:
                logger.exception(m18n.n('restore_app_failed', app=app_id))
                # Cleaning app directory
                shutil.rmtree(app_setting_path, ignore_errors=True)
            else:
                result['apps'].append(app_id)
            finally:
                filesystem.rm(tmp_script, force=True)

    # Check if something has been restored
    if not result['hooks'] and not result['apps']:
        _clean_tmp_dir(1)
        raise MoulinetteError(errno.EINVAL, m18n.n('restore_nothings_done'))
    if result['apps']:
        app_ssowatconf(auth)

    _clean_tmp_dir()
    logger.success(m18n.n('restore_complete'))

    return result


def backup_list(with_info=False, human_readable=False):
    """
    List available local backup archives

    Keyword arguments:
        with_info -- Show backup information for each archive
        human_readable -- Print sizes in human readable format

    """
    result = []

    try:
        # Retrieve local archives
        archives = os.listdir(archives_path)
    except OSError:
        logger.debug("unable to iterate over local archives", exc_info=1)
    else:
        # Iterate over local archives
        for f in archives:
            try:
                name = f[:f.rindex('.tar.gz')]
            except ValueError:
                continue
            result.append(name)
        result.sort()

    if result and with_info:
        d = OrderedDict()
        for a in result:
            d[a] = backup_info(a, human_readable=human_readable)
        result = d

    return { 'archives': result }


def backup_info(name, with_details=False, human_readable=False):
    """
    Get info about a local backup archive

    Keyword arguments:
        name -- Name of the local backup archive
        with_details -- Show additional backup information
        human_readable -- Print sizes in human readable format

    """
    archive_file = '%s/%s.tar.gz' % (archives_path, name)
    if not os.path.isfile(archive_file):
        raise MoulinetteError(errno.EIO,
            m18n.n('backup_archive_name_unknown', name=name))

    info_file = "%s/%s.info.json" % (archives_path, name)
    try:
        with open(info_file) as f:
            # Retrieve backup info
            info = json.load(f)
    except:
        # TODO: Attempt to extract backup info file from tarball
        logger.debug("unable to load '%s'", info_file, exc_info=1)
        raise MoulinetteError(errno.EIO, m18n.n('backup_invalid_archive'))

    # Retrieve backup size
    size = info.get('size', 0)
    if not size:
        tar = tarfile.open(archive_file, "r:gz")
        size = reduce(lambda x,y: getattr(x, 'size', x)+getattr(y, 'size', y),
                      tar.getmembers())
        tar.close()
    if human_readable:
        size = binary_to_human(size) + 'B'

    result = {
        'path': archive_file,
        'created_at': time.strftime(m18n.n('format_datetime_short'),
                                    time.gmtime(info['created_at'])),
        'description': info['description'],
        'size': size,
    }

    if with_details:
        for d in ['apps', 'hooks']:
            result[d] = info[d]
    return result


def backup_delete(name):
    """
    Delete a backup

    Keyword arguments:
        name -- Name of the local backup archive

    """
    hook_callback('pre_backup_delete', args=[name])

    archive_file = '%s/%s.tar.gz' % (archives_path, name)

    info_file = "%s/%s.info.json" % (archives_path, name)
    for backup_file in [archive_file,info_file]:
        if not os.path.isfile(backup_file):
            raise MoulinetteError(errno.EIO,
                m18n.n('backup_archive_name_unknown', name=backup_file))
        try:
            os.remove(backup_file)
        except:
            logger.debug("unable to delete '%s'", backup_file, exc_info=1)
            raise MoulinetteError(errno.EIO,
                m18n.n('backup_delete_error', path=backup_file))

    hook_callback('post_backup_delete', args=[name])

    logger.success(m18n.n('backup_deleted'))
