# -*- coding: utf-8 -*-
# Copyright (C) 2010 Eric Allen
#
# Author: Eric Allen <eric@hackerengineer.net>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

# Dripbox: Keep remote copy of directory tree in sync with local tree

import sys
import os
import re
import logging
import time
import getpass
import subprocess
import socket  # to catch socket errors
import errno

import paramiko
import fsevents
from fsevents import Observer, Stream

SSH_KEY = os.path.join(os.environ['HOME'], ".ssh", "id_rsa")
SSH_CONFIG = os.path.join(os.environ['HOME'], ".ssh", "config")
LOCAL_PATH = os.getcwd()

log = logging.getLogger("dripbox")

# globals
remote_root = None
sftp_client = None


def _get_ssh_config_port(host):
    if not os.path.exists(SSH_CONFIG):
        return None

    ssh_config = paramiko.SSHConfig()
    try:
        with open(SSH_CONFIG, 'r') as cfile:
            ssh_config.parse(cfile)
    except OSError, e:
        log.error("Could not open SSH config: %s", str(e))
        return None
    except Exception, e:
        log.error("Problem parsing SSH config: %s %s", type(e), str(e))
        return None

    port = ssh_config.lookup(host).get('port')
    port = port and int(port)
    return port


def rsync(remote, host, port=None, sync=False):
    if not port:
        port = _get_ssh_config_port(host) or 22

    if sync:
        command = ["rsync", "--delete", "-rltvze", "ssh -p%s" % port,
                   "--exclude", ".git", ".", remote]
        subprocess.check_call(command)
    else:
        command = ["rsync", "--delete", "-crnltvze", "ssh -p%s" % port,
                   "--exclude", ".git", ".", remote]
        diff = subprocess.Popen(command, stdout=subprocess.PIPE)
        output, _ = diff.communicate()
        for line in output.split("\n"):
            if line == "":
                pass
            elif line == "sending incremental file list":
                pass
            elif re.match("sent \d+ bytes +received \d+ bytes  [0-9\.]+ bytes/sec", line):
                pass
            elif re.search("total size is \d+ +speedup is [0-9\.]", line):
                pass
            else:
                print output
                print "WARNING: The remote tree is out of sync with the local tree. This is a dangerous situation."
                print "Run dripbox with -f if you know what you're doing and want to run dripbox anyway"
                print "We recommend you use --sync instead."
                raise SystemExit(1)


def launch(username_p, host_p, remote_path, port_p=None):
    global remote_root, sftp_client, username, host, port
    username = username_p
    host = host_p
    port = port_p

    remote_root = remote_path
    sftp_client = setup_transport(username, host, port)
    dirs_to_watch = [entry for entry in os.listdir(LOCAL_PATH) if
            os.path.isdir(entry) and not entry.startswith(".")]
    watch_files(dirs_to_watch)


def get_ssh_key():
    agent = paramiko.Agent()
    if agent.keys:
        # TODO: support having more than one key loaded
        if len(agent.keys) > 1:
            print ("You have more than one key in your SSH Agent"
                   " — using the first found!")
        return agent.keys[0]

    try:
        return paramiko.RSAKey.from_private_key_file(SSH_KEY)
    except paramiko.PasswordRequiredException:
        passwd = getpass.getpass("Enter passphrase for %s: " % SSH_KEY)
        try:
            return paramiko.RSAKey.from_private_key_file(filename=SSH_KEY,
                                                         password=passwd)
        except paramiko.SSHException:
            print "Could not read private key; bad password?"
            raise SystemExit(1)


def setup_transport(username, host, port=None):
    if not port:
        port = _get_ssh_config_port(host) or 22

    try:
        transport = paramiko.Transport((host, port))
    except socket.gaierror, e:
        sys.stderr.write("Couldn't connect to %s:%s (%s)\n"
                         % (host, port, str(e)))
        raise SystemExit(1)

    key = get_ssh_key()
    transport.connect(username=username, pkey=key)
    client = paramiko.SFTPClient.from_transport(transport)
    client.get_channel().settimeout(5)
    return client

def reconnect():
    log.warn("Re-connecting")
    global sftp_client, username, host, port
    sftp_client = setup_transport(username, host, port)


def is_temp_file(path):
    if path.endswith(".swp"):
        return True
    if path.endswith("~"):
        return True
    if path.startswith(".#"):
        return True
    return False


def update_file(event):
    global remote_root, sftp_client
    full_path = event.name
    if is_temp_file(full_path):
        return
    # Trying to sync git stuff can put remote repo into a really weird state
    if ".git" in full_path:
        return

    mask = event.mask
    truncated_path = full_path.replace(LOCAL_PATH, "")
    remote_path = remote_root + truncated_path

    try:
        if mask & fsevents.IN_DELETE:
            log.info("Deleting %s" % full_path)
            try:
                if os.path.isdir(full_path):
                    sftp_client.rmdir(remote_path)
                else:
                    sftp_client.remove(remote_path)
            except IOError:
                log.info("File was already deleted")
        else:
            if os.path.isdir(full_path):
                log.info("Creating directory %s" % remote_path)
                try:
                    sftp_client.mkdir(remote_path)
                except IOError:
                    log.info("Directory already exists")
            else:
                log.info("Uploading %s to %s" % (full_path, remote_path))
                try:
                    sftp_client.put(full_path, remote_path)
                except EOFError, e:
                    log.warn("Couldn't upload file")
                    time.sleep(0.1)
                    sftp_client.put(full_path, remote_path)
                except OSError, e:
                    log.warn("Couldn't upload file")
                    time.sleep(0.1)
                    try:
                        sftp_client.put(full_path, remote_path)
                    except OSError, e:
                        log.exception("Failed to upload file: %s" % e)
        log.info("Done uploading %s", full_path)
    except paramiko.SSHException:
        reconnect()
        update_file(event)
    except EOFError:
        reconnect()
        update_file(event)
    except socket.timeout:
        reconnect()
        update_file(event)
    except IOError, e:
        if e.errno == errno.EACCES:
            log.error("PERMISSION DENIED writing to %s", remote_path)
            log.error("Dripbox was UNABLE TO SYNC %s to %s", full_path, remote_path)
        else:
            log.error("IOError: %s", str(e))
            reconnect()
            update_file(event)

def watch_files(paths):
    global observer
    observer = Observer()
    stream = Stream(update_file, file_events=True, *paths)
    observer.schedule(stream)
    log.info("Starting observer")
    observer.daemon = True
    observer.start()
    log.info("Observer started")
