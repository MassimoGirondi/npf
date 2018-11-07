import multiprocessing
import os
import time
from multiprocessing import Queue
from typing import List
import paramiko
from .executor import Executor
from ..eventbus import EventBus
from paramiko.buffered_pipe import PipeTimeout
import socket

class SSHExecutor(Executor):

    def __init__(self, user, addr, path):
        super().__init__()
        self.user = user
        self.addr = addr
        self.path = path
        #Executor should not make any connection in init as parameters can be overwritten afterward

    def get_connection(self):
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self.addr, username=self.user)
        return ssh


    def exec(self, cmd, bin_paths : List[str] = None, queue: Queue = None, options = None, stdin = None, timeout=None, sudo=False, testdir=None, event=None, title=None, env={}):
        if not title:
            title = self.addr
        if not event:
            event = EventBus()
        path_list = [p if os.path.isabs(p) else self.path+'/'+p for p in (bin_paths if bin_paths is not None else [])]
        if options and options.show_cmd:
            print("Executing on %s%s (PATH+=%s) :\n%s" % (self.addr,(' with sudo' if sudo and self.user != "root" else ''),':'.join(path_list), cmd.strip()))

        pre = 'cd '+ self.path + '\n'

        if self.path:
            env['NPF_ROOT'] = self.path

        for k,v in env.items():
            if v is not None:
                pre += 'export ' + k + '='+v+'\n'
        if path_list:
            path_cmd = 'export PATH="%s:$PATH"\n' % (':'.join(path_list))
        else:
            path_cmd = ''

        if sudo and self.user != "root":
            cmd = "sudo -E bash -c '"+path_cmd + cmd.replace("'", "\\'") + "'";
        else:
            pre = path_cmd + pre

        try:
            ssh = self.get_connection()

            ssh_stdin, ssh_stdout, ssh_stderr = ssh.exec_command(pre + cmd,timeout=timeout, get_pty=True)

            if stdin is not None:
                ssh_stdin.write(stdin)

            channels = [ssh_stdout, ssh_stderr]
            output = ['','']

            pid = os.getpid()
            step = 0.2
            for channel in channels:
                channel.channel.setblocking(False)

            while not event.is_terminated() and not ssh_stdout.channel.exit_status_ready():
                try:
                    line = None
                    while ssh_stdout.channel.recv_ready() or ssh_stderr.channel.recv_ready():
                        for ichannel,channel in enumerate(channels[:1]):
                            if channel.channel.recv_ready():
                                 line = channel.readline()
                                 if options and options.show_full:
                                    self._print(title, line, False)
                                 self.searchEvent(line, event)
                                 output[ichannel] += line
                    else:
                        event.wait_for_termination(step)
                        if timeout is not None:
                            timeout -= step
                except PipeTimeout:
                    pass
                except socket.timeout:
                    self._print(title, "Interrupted by timeout", True)
                    event.wait_for_termination(step)
                    if timeout is not None:
                        timeout -= step
                except KeyboardInterrupt:
                    event.terminate()
                    return -1, out, err
                if timeout is not None:
                    if timeout < 0:
                        event.terminate()
                        pid = 0
                        break
            if event.is_terminated():
                if not ssh_stdin.channel.closed:
                    ssh_stdin.channel.send(chr(3))
                    i=0
                    ssh_stdout.channel.status_event.wait(timeout=1)
                ret = 0 #Ignore return code because we kill it before completion.
                ssh.close()
            else:
                ret = ssh_stdout.channel.recv_exit_status()

            for ichannel,channel in enumerate(channels):
                for line in channel.readlines():
                    if options and options.show_full:
                        self._print(title, line, False)
                    self.searchEvent(line, event)
                    output[ichannel] += line

            return pid,output[0], output[1],ret
        except paramiko.ssh_exception.SSHException as e:
            print("Error while connecting to %s", self.addr)
            raise e

    def writeFile(self,filename,path_to_root,content):
        f = open(filename, "w")
        f.write(content)
        f.close()

        try:
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.load_system_host_keys()
                try:
                    ssh.connect(self.addr, 22, username=self.user)
                except Exception as e:
                    print("Cannot connect to %s with username %s" % (self.addr,self.user))
                    raise e

                transport = ssh.get_transport()
                with transport.open_channel(kind='session') as channel:
                    channel.exec_command('mkdir -p %s/%s' % (self.path, path_to_root))
                    if channel.recv_exit_status() != 0:
                        return False
                with transport.open_channel(kind='session') as channel:
                    channel.exec_command('cat > %s/%s/%s' % (self.path,path_to_root,filename))
                    channel.sendall(content)
                    channel.shutdown_write()
                    return channel.recv_exit_status() == 0
        except paramiko.ssh_exception.SSHException as e:
            print("Error while connecting to %s", self.addr)
            raise e

    def sendFolder(self, path):
        try:
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.load_system_host_keys()
                try:
                    ssh.connect(self.addr, 22, username=self.user)
                except Exception as e:
                    print("Cannot connect to %s with username %s" % (self.addr,self.user))
                    raise e

                transport = ssh.get_transport()

                sftp = paramiko.SFTPClient.from_transport(transport)

                ignored = ['.git', '.vimhistory']
                def _send(path):
                    rlist = sftp.listdir(self.path + path)

                    for entry in os.scandir(path):
                        if entry.is_file():
                            if not entry.name in rlist:
                                try:
                                    remote = self.path + path + '/' + entry.name
                                    sftp.put(path + '/' + entry.name, remote)
                                    sftp.chmod(remote, entry.stat().st_mode)
                                except FileNotFoundError:
                                    raise(Exception("Could not send %s to %s"  % (path + '/' + entry.name, remote)))
                        else:
                            if entry.name in ignored:
                                continue
                            if entry.name not in rlist:
                                sftp.mkdir(self.path + path + '/' + entry.name)
                                _send(path +'/'+entry.name + '/')
                curpath = ''
                for d in path.split('/'):
                    curpath = curpath + d + '/'
                    try:
                        sftp.stat(self.path + '/' + curpath)
                    except FileNotFoundError:
                        sftp.mkdir(self.path + '/' + curpath)

                _send(path)

                sftp.close()

        except paramiko.ssh_exception.SSHException as e:
            print("Error while connecting to %s", self.addr)
            raise e
