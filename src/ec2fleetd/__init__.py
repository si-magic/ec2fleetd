import datetime
import io
import json
import os
import re
import subprocess
import sys
from abc import *
from contextlib import ContextDecorator
from typing import Any, Callable, Iterable

from ec2fleetd import magic

ver = "0.0.0"

def pexcept (e: Exception, msg: str = None) -> int:
	if msg:
		ret = sys.stderr.write(
			"{msg}: {e}{nl}".format(msg = msg, e = e, nl = os.linesep))
	else:
		ret = sys.stderr.write("{e}{nl}".format(e = e, nl = os.linesep))

	return ret

def mk_transc_idempt (transc: str):
	return magic.AWSTags.TRANSACTION_ID + "=" + transc

def parse_bool (s: str) -> bool:
	s = s.lower()
	if s == "true":
		return True
	if s == "false":
		return False

	return float(s) != 0

class AttachCase:
	NOOP = "noop"
	create = "create"
	pool = "pool"
	new = "new"

class DaemonState:
	FAILED = "failed"
	STARTING = "starting"
	STARTED = "started"
	STOPPING = "stopping"
	INTERRUPTED = "interrupted"

class MacroSet:
	def __init__ (self):
		self.domain: str = ""
		self.instance_id: str = ""
		self.instance_type: str = ""
		self.instance_index: int = None
		self.placement_region: str = None
		self.placement_zone: str = None
		self.hypervisor: str = None
		self.primary_public_ipv4: str = None
		self.primary_public_ipv6: str = None
		self.public_ipv4_list: list[str] = list[str]()
		self.public_ipv6_list: list[str] = list[str]()
		self.static_dns_rr: list[str] = list[str]()
		self.attach_source: str = None
		self.attach_op: str = None
		self.volume_id: str = None
		self.volume_pool: str = None
		self.attached_device: str = None
		self.daemon_state: str = ""
		self.error = list[str]()
		self.interrupt_action = None
		self.interrupt_time = None
		self.transaction_id: str = None
		self.transaction_log = list[ResourceTransactionLog]()

	def dict (self, json_obj = False) -> dict[str, Any]:
		if json_obj:
			transc_f = lambda x: [ log.dict() for log in x ]
		else:
			transc_f = lambda x: x

		return {
			"domain": self.domain,
			"instance_id": self.instance_id,
			"instance_type": self.instance_type,
			"instance_index": self.instance_index,
			"placement_region": self.placement_region,
			"placement_zone": self.placement_zone,
			"hypervisor": self.hypervisor,
			"primary_public_ipv4": self.primary_public_ipv4,
			"primary_public_ipv6": self.primary_public_ipv6,
			"public_ipv4_list": self.public_ipv4_list,
			"public_ipv6_list": self.public_ipv6_list,
			"static_dns_rr": self.static_dns_rr,
			"attach_source": self.attach_source,
			"attach_op": self.attach_op,
			"volume_id": self.volume_id,
			"volume_pool": self.volume_pool,
			"attached_device": self.attached_device,
			"daemon_state": self.daemon_state,
			"error": self.error,
			"interrupt_action": self.interrupt_action,
			"interrupt_time": self.interrupt_time,
			"transaction_id": self.transaction_id,
			"transaction_log": transc_f(self.transaction_log),
			"cwd": os.getcwd(),
			"ts": datetime.datetime.now().astimezone().isoformat(),
			"pid": os.getpid()
		}

	def format(self, s: str) -> str:
		mask_none = lambda x: "" if x is None else str(x)
		join_comma = lambda x: ", ".join(x if x is not None else [])
		dump_json = lambda x: json.dumps(x, indent = '\t')

		all_json = self.dict(True)

		return s.format(
			all_json = dump_json(all_json),

			domain = all_json["domain"],
			instance_id = all_json["instance_id"],
			instance_type = all_json["instance_type"],
			instance_index = mask_none(all_json["instance_index"]),
			placement_region = mask_none(all_json["placement_region"]),
			placement_zone = mask_none(all_json["placement_zone"]),
			hypervisor = mask_none(all_json["hypervisor"]),
			primary_public_ipv4 = mask_none(all_json["primary_public_ipv4"]),
			primary_public_ipv6 = mask_none(all_json["primary_public_ipv6"]),
			public_ipv4_list = join_comma(all_json["public_ipv4_list"]),
			public_ipv6_list = join_comma(all_json["public_ipv6_list"]),
			static_dns_rr = join_comma(all_json["static_dns_rr"]),
			attach_source = mask_none(all_json["attach_source"]),
			attach_op = mask_none(all_json["attach_op"]),
			volume_id = mask_none(all_json["volume_id"]),
			volume_pool = mask_none(all_json["volume_pool"]),
			attached_device = mask_none(all_json["attached_device"]),
			daemon_state = all_json["daemon_state"],
			error = all_json["error"],
			interrupt_action = mask_none(all_json["interrupt_action"]),
			interrupt_time = mask_none(all_json["interrupt_time"]),
			transaction_id = all_json["transaction_id"],
			transaction_log = dump_json(all_json["transaction_log"]),
			cwd = all_json["cwd"],
			ts = all_json["ts"],
			pid = all_json["pid"],
		)

class ResourceTransactionLog (ABC):
	'''Represents a resource transaction that's been done (by
	creating/modification/updating)'''
	@abstractmethod
	def dict (self) -> dict: ...
	@abstractmethod
	def dry (self) -> bool: ...

class ResourceHold (ABC):
	'''Represents a resource that's been created as a result of daemon
	initialisation. Used to revert resources to the states as they were in
	before the daemon attempted to initialise the instance in case of failure
	during the process.'''
	@abstractmethod
	def rollback (self): ...
	@abstractmethod
	def commit (self): ...

class ResourceTransactionLogger:
	def __init__ (self):
		self.logs = list[ResourceTransactionLog]()
		self.cbset = set[Callable[[Iterable[ResourceTransactionLog]], Any]]()

	def publish (self, logs: Iterable[ResourceTransactionLog]):
		self.logs += logs
		for cb in self.cbset:
			cb(logs)

class TransientResourceManager (ContextDecorator):
	def __init__ (
			self,
			critical: bool,
			parent = None):
		self._hold = list[ResourceHold]()
		self._critical = critical
		self._parent = parent

	def __enter__ (self):
		return self

	def __exit__ (self, *exc):
		if exc[0]:
			if self._critical:
				if self._parent:
					self.move(self._parent)
				else:
					self.rollback()

				return False
			else:
				self.rollback()
				if self._parent:
					self.move(self._parent)

				return True

		if self._parent:
			self.move(self._parent)
		else:
			self.commit()

		return False

	def move (self, other):
		other._hold += self._hold
		self._hold.clear()

	def push (self, rt: ResourceHold):
		self._hold.append(rt)

	def push (self, rt: Iterable[ResourceHold]):
		self._hold += rt

	def commit (self):
		for rt in self._hold:
			rt.commit()

		self._hold.clear()

	def rollback (self):
		self._hold.reverse()
		for rt in self._hold:
			try:
				rt.rollback()
			except:
				pass

		self._hold.clear()

class ExitCodeCheck:
	class RE:
		RANGE = re.compile('''^(\\d+)(?:\\s+)?(?:-(?:\\s+)?(\\d+))?$''')

	def __init__ (self, s: str | None = None):
		def raise_invalid ():
			raise ValueError(s + ": invalid exit code range")

		self._s: set[range] | None

		if s is None:
			self._s = None
		else:
			self._s = set[range]()

			for v in s.split(','):
				m = ExitCodeCheck.RE.RANGE.match(v.strip())
				if m:
					start = int(m.group(1))
					end = m.group(2)
					if end is not None:
						end = int(end)
					else:
						end = start + 1

					if start > end:
						raise_invalid()

					self._s.add(range(start, end))
				else:
					raise_invalid()

	def __str__ (self) -> str:
		return str(self._s)

	def check (self, c: int) -> bool:
		if self._s is None:
			return True

		for r in self._s:
			if c in r:
				return True

		return False

class Exec:
	def __init__ (self, argv: Iterable[str], eccs: str | None = None):
		self._argv = list[str](argv)
		self._ecc = ExitCodeCheck(eccs)

	def do_exec (self) -> int:
		with subprocess.Popen(self._argv) as p:
			ret = p.wait()
			self.raise_exitcode(ret)
			return ret

	def check_exitcode (self, ec: int) -> bool:
		return self._ecc.check(ec)

	def raise_exitcode (self, ec: int, pid = None):
		if self.check_exitcode(ec):
			return
		cmd = ' '.join([ '"{arg}"'.format(arg = arg) for arg in self._argv ])
		if pid is None:
			pid = ""

		raise ChildProcessError(
'''{cmd}[{pid}]: returned {ec}, not in {range}'''.format(
					cmd = cmd,
					pid = pid,
					ec = ec,
					range = self._ecc))

def init_exec_mat (
		it: Iterable[dict],
		trans_f: Callable[[str], str]) -> tuple[
			list[Exec],
			dict[str, list[Exec]]]:
	m = dict[str, list[Exec]]()
	all = list[Exec]()

	for spec in it:
		l_ec = list[Exec]()

		for line in spec["lines"]:
			argv = [ trans_f(arg) for arg in line["argv"] ]
			ec = Exec(argv, line.get("ec", "0"))
			l_ec.append(ec)

		l_event = spec.get("on")
		if l_event is None:
			all += l_ec
		else:
			for on in l_event:
				if on in m:
					m[on] = l_ec
				else:
					m[on] += l_ec

	return ( all, m )

def do_exec_mat (
		mat: tuple[list[Exec], dict[str, list[Exec]]],
		evt: str | None = None):
	l = list[Exec](mat[0])

	if evt is not None:
		l += mat[1].get(evt, [])

	for exec in l:
		exec.do_exec()

class NotifyBackend (ABC):
	@abstractmethod
	def post (self, subject: str, body: str): ...

class InterruptSchedule (ABC):
	@abstractmethod
	def __bool__ (self) -> bool: ...
	@abstractmethod
	def time (self) -> datetime.datetime | None: ...
	@abstractmethod
	def action (self) -> str | None: ...
	@abstractmethod
	def valid (self) -> bool: ...

class MetaManager (ABC):
	@abstractmethod
	def update_macroset (meta: Any, ms: MacroSet): ...

	@abstractmethod
	def fetch_meta (self): ...
	@abstractmethod
	def open_userdata (self) -> io.BufferedIOBase: ...
	@abstractmethod
	def poll_int_sched (self) -> InterruptSchedule: ...
