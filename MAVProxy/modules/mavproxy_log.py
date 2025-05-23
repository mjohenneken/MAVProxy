'''
log command handling

AP_FLAKE8_CLEAN
'''

import os
import time

from MAVProxy.modules.lib import mp_module


class LogModule(mp_module.MPModule):
    def __init__(self, mpstate):
        super(LogModule, self).__init__(mpstate, "log", "log transfer")
        self.add_command('log', self.cmd_log, "log file handling", ['<download|status|erase|resume|cancel|list>'])
        self.reset()

    def reset(self):
        self.download_set = set()
        self.download_file = None
        self.download_lognum = None
        self.download_filename = None
        self.download_start = None
        self.download_last_timestamp = None
        self.download_ofs = 0
        self.retries = 0
        self.entries = {}
        self.download_queue = []
        self.last_status = time.time()

    def mavlink_packet(self, m):
        '''handle an incoming mavlink packet'''
        if m.get_type() == 'LOG_ENTRY':
            self.handle_log_entry(m)
        elif m.get_type() == 'LOG_DATA':
            self.handle_log_data(m)

    def handle_log_entry(self, m):
        '''handling incoming log entry'''
        if m.time_utc == 0:
            tstring = ''
        else:
            tstring = time.ctime(m.time_utc)
        if m.num_logs == 0:
            print("No logs")
            return
        self.entries[m.id] = m
        print("Log %u  numLogs %u lastLog %u size %u %s" % (m.id, m.num_logs, m.last_log_num, m.size, tstring))

    def handle_log_data(self, m):
        '''handling incoming log data'''
        if self.download_file is None:
            return
        # lose some data
        # import random
        # if random.uniform(0,1) < 0.05:
        #    print('dropping ', str(m))
        #    return
        if m.ofs != self.download_ofs:
            self.download_file.seek(m.ofs)
            self.download_ofs = m.ofs
        if m.count != 0:
            s = bytearray(m.data[:m.count])
            self.download_file.write(s)
            self.download_set.add(m.ofs // 90)
            self.download_ofs += m.count
        self.download_last_timestamp = time.time()
        if m.count == 0 or (m.count < 90 and len(self.download_set) == 1 + (m.ofs // 90)):
            dt = time.time() - self.download_start
            self.download_file.close()
            size = os.path.getsize(self.download_filename)
            speed = size / (1000.0 * dt)
            status = (
                f"Finished downloading {self.download_filename} " +
                f"({size} bytes {dt:0.1f} seconds, " +
                f"{speed:.1f} kbyte/sec " +
                f"{self.retries} retries)"
            )
            self.console.set_status('LogDownload', status, row=4)
            print(status)
            self.download_file = None
            self.download_filename = None
            self.download_set = set()
            self.master.mav.log_request_end_send(
                self.target_system,
                self.target_component
            )
            if len(self.download_queue):
                self.log_download_next()
        self.update_status()

    def handle_log_data_missing(self):
        '''handling missing incoming log data'''
        if len(self.download_set) == 0:
            return
        highest = max(self.download_set)
        diff = set(range(highest)).difference(self.download_set)
        if len(diff) == 0:
            self.master.mav.log_request_data_send(
                self.target_system,
                self.target_component,
                self.download_lognum,
                (1 + highest) * 90,
                0xffffffff
            )
            self.retries += 1
        else:
            num_requests = 0
            while num_requests < 20:
                start = min(diff)
                diff.remove(start)
                end = start
                while end + 1 in diff:
                    end += 1
                    diff.remove(end)
                self.master.mav.log_request_data_send(
                    self.target_system,
                    self.target_component,
                    self.download_lognum,
                    start * 90,
                    (end + 1 - start) * 90
                )
                num_requests += 1
                self.retries += 1
                if len(diff) == 0:
                    break

    def log_status(self, console=False):
        '''show download status'''
        if self.download_filename is None:
            print("No download")
            return
        dt = time.time() - self.download_start
        speed = os.path.getsize(self.download_filename) / (1000.0 * dt)
        m = self.entries.get(self.download_lognum, None)
        file_size = os.path.getsize(self.download_filename)
        if m is None:
            size = 0
            pct = 0
        elif m.size == 0:
            size = 0
            pct = 100
        else:
            size = m.size
            pct = (100.0*file_size)/size
        highest = 0
        if len(self.download_set):
            highest = max(self.download_set)
        diff = set(range(highest)).difference(self.download_set)
        status = (
            f"Downloading {self.download_filename} - " +
            f"{os.path.getsize(self.download_filename)}/{size} bytes " +
            f"{pct:.1f}% {speed:.1f} kbyte/s " +
            f"({self.retries} retries {len(diff)} missing)"
        )
        if console:
            self.console.set_status('LogDownload', status, row=4)
        else:
            print(status)

    def log_download_next(self):
        if len(self.download_queue) == 0:
            return
        latest = self.download_queue.pop()
        filename = self.default_log_filename(latest)
        if os.path.isfile(filename) and os.path.getsize(filename) == self.entries.get(latest).to_dict()["size"]:
            print("Skipping existing %s" % (filename))
            self.log_download_next()
        else:
            self.log_download(latest, filename)

    def log_download_all(self):
        if len(self.entries.keys()) == 0:
            print("Please use log list first")
            return
        self.download_queue = sorted(self.entries, key=lambda id: self.entries[id].time_utc)
        self.log_download_next()

    def log_download_range(self, first, last):
        self.download_queue = sorted(list(range(first, last+1)), reverse=True)
        print(self.download_queue)
        self.log_download_next()

    def log_download_from(self, fromnum=0):
        if len(self.entries.keys()) == 0:
            print("Please use log list first")
            return
        self.download_queue = sorted(self.entries, key=lambda id: self.entries[id].time_utc)
        self.download_queue = self.download_queue[fromnum:len(self.download_queue)]
        self.log_download_next()

    def log_download(self, log_num, filename):
        '''download a log file'''
        print("Downloading log %u as %s" % (log_num, filename))
        self.download_lognum = log_num
        self.download_file = open(filename, "wb")
        self.master.mav.log_request_data_send(
            self.target_system,
            self.target_component,
            log_num,
            0,
            0xFFFFFFFF
        )
        self.download_filename = filename
        self.download_set = set()
        self.download_start = time.time()
        self.download_last_timestamp = time.time()
        self.download_ofs = 0
        self.retries = 0

    def default_log_filename(self, log_num):
        return "log%u.bin" % log_num

    def cmd_log(self, args):
        '''log commands'''
        usage = "usage: log <list|download|erase|resume|status|cancel>"
        if len(args) < 1:
            print(usage)
            return

        if args[0] == "status":
            self.log_status()
        elif args[0] == "list":
            print("Requesting log list")
            self.download_set = set()
            self.master.mav.log_request_list_send(
                self.target_system,
                self.target_component,
                0,
                0xffff
            )

        elif args[0] == "erase":
            self.master.mav.log_erase_send(
                self.target_system,
                self.target_component
            )

        elif args[0] == "resume":
            self.master.mav.log_request_end_send(
                self.target_system,
                self.target_component
            )

        elif args[0] == "cancel":
            if self.download_file is not None:
                self.download_file.close()
            self.reset()

        elif args[0] == "download":
            if len(args) < 2:
                print("usage: log download all | log download <lognumber> <filename> | log download from <lognumber>|log download range FIRST LAST") # noqa:E501
                return
            if args[1] == 'all':
                self.log_download_all()
                return
            if args[1] == 'from':
                if len(args) < 2:
                    args[2] == 0
                self.log_download_from(int(args[2]))
                return
            if args[1] == 'range':
                if len(args) < 2:
                    print("Usage: log download range FIRST LAST")
                    return
                self.log_download_range(int(args[2]), int(args[3]))
                return
            if args[1] == 'latest':
                if len(self.entries.keys()) == 0:
                    print("Please use log list first")
                    return
                log_num = sorted(self.entries, key=lambda id: self.entries[id].time_utc)[-1]
            else:
                log_num = int(args[1])
            if len(args) > 2:
                filename = args[2]
            else:
                filename = self.default_log_filename(log_num)
            self.log_download(log_num, filename)
        else:
            print(usage)

    def update_status(self):
        '''update log download status in console'''
        now = time.time()
        if self.download_file is not None and now - self.last_status > 0.5:
            self.last_status = now
            self.log_status(True)

    def idle_task(self):
        '''handle missing log data'''
        if self.download_last_timestamp is not None and time.time() - self.download_last_timestamp > 0.7:
            self.download_last_timestamp = time.time()
            self.handle_log_data_missing()
        self.update_status()


def init(mpstate):
    '''initialise module'''
    return LogModule(mpstate)
