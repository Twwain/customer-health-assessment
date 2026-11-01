"""Bounded persisted runs recover after the process or executor disappears."""
import ctypes
import os
import threading
from uuid import uuid4
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session
from database import utcnow
from models import AIAssessmentRun
from . import audit

INSTANCE = uuid4().hex
PENDING = ("capturing", "facts_ready", "request_resolved", "planning", "validating", "repairing", "validated", "committing")
_active = set()
_lock = threading.Lock()


def register(run_id, seconds):
    with _lock:
        _active.add(run_id)
    return dict(owner=INSTANCE, pid=os.getpid(), deadline_at=(utcnow()+timedelta(seconds=seconds)).isoformat())


def unregister(run_id):
    with _lock:
        _active.discard(run_id)


def executor_alive(run_id, runtime):
    pid = runtime.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        with _lock:
            return runtime.get("owner") == INSTANCE and run_id in _active
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not evidence of death; the persisted deadline
            # still guarantees a finite terminal state.
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def recover_pending(db, session_ids=None):
    query = db.query(AIAssessmentRun).filter(AIAssessmentRun.status.in_(PENDING))
    if session_ids is not None:
        query = query.filter(AIAssessmentRun.session_id.in_(session_ids))
    changed = 0
    for observed in query.all():
        run_id,observed_updated=observed.run_id,observed.updated_at
        runtime = observed.versions.get("runtime", {})
        try:
            expired = datetime.fromisoformat(runtime["deadline_at"]) <= utcnow()
        except (KeyError, TypeError, ValueError):
            expired = False
        if not expired and executor_alive(run_id, runtime):
            continue
        status, error = ("timeout", "timeout") if expired else ("provider_error", "executor_lost")
        with Session(bind=getattr(db.get_bind(),'engine',db.get_bind())) as writer:
            audit.begin_write(writer)
            run=writer.get(AIAssessmentRun,run_id)
            if not run or run.status not in PENDING or run.updated_at!=observed_updated or run.versions.get('runtime',{})!=runtime:
                writer.rollback()
                continue
            changed+=int(audit.apply_locked(writer,run,'recover',status=status,error_code=error))
            writer.commit()
    if changed:
        for value in list(db.identity_map.values()):
            if isinstance(value,AIAssessmentRun):
                db.expire(value)
    return changed
