from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Set

# Documented CoreServices constants.
_FLAG_MUST_SCAN_SUBDIRS = 0x00000001
_FLAG_USER_DROPPED = 0x00000002
_FLAG_KERNEL_DROPPED = 0x00000004
_FLAG_EVENT_IDS_WRAPPED = 0x00000008
_FLAG_HISTORY_DONE = 0x00000010
_FLAG_ROOT_CHANGED = 0x00000020

_CREATE_FLAG_NO_DEFER = 0x00000002
_CREATE_FLAG_WATCH_ROOT = 0x00000004
_CREATE_FLAG_FILE_EVENTS = 0x00000010

_UTF8 = 0x08000100
_EVENT_ID_NOT_FOUND = 0

DROP_FLAGS = _FLAG_USER_DROPPED | _FLAG_KERNEL_DROPPED | _FLAG_EVENT_IDS_WRAPPED


def _load() -> tuple:
    try:
        cf_name = ctypes.util.find_library("CoreFoundation")
        cs_name = ctypes.util.find_library("CoreServices")
        if not cf_name or not cs_name:
            return (None, None)
        return (ctypes.CDLL(cf_name, use_errno=True), ctypes.CDLL(cs_name, use_errno=True))
    except OSError:
        return (None, None)


_CF, _CS = _load()
FSEVENTS_AVAILABLE = _CF is not None and _CS is not None

if FSEVENTS_AVAILABLE:
    _CFIndex = ctypes.c_long
    _EventId = ctypes.c_uint64

    _CF.CFStringCreateWithCString.restype = ctypes.c_void_p
    _CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _CF.CFStringGetCString.restype = ctypes.c_bool
    _CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _CF.CFArrayCreate.restype = ctypes.c_void_p
    _CF.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), _CFIndex, ctypes.c_void_p]
    _CF.CFRelease.argtypes = [ctypes.c_void_p]
    _CF.CFUUIDCreateString.restype = ctypes.c_void_p
    _CF.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _CF.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    _CF.CFRunLoopRunInMode.restype = ctypes.c_int32
    _CF.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]

    _RUN_LOOP_DEFAULT_MODE = ctypes.c_void_p.in_dll(_CF, "kCFRunLoopDefaultMode")
    _ARRAY_CALLBACKS = ctypes.c_void_p.in_dll(_CF, "kCFTypeArrayCallBacks")

    _CS.FSEventsGetCurrentEventId.restype = _EventId
    _CS.FSEventsGetCurrentEventId.argtypes = []
    _CS.FSEventsGetLastEventIdForDeviceBeforeTime.restype = _EventId
    _CS.FSEventsGetLastEventIdForDeviceBeforeTime.argtypes = [ctypes.c_int32, ctypes.c_double]
    _CS.FSEventsCopyUUIDForDevice.restype = ctypes.c_void_p
    _CS.FSEventsCopyUUIDForDevice.argtypes = [ctypes.c_int32]

    _CALLBACK = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(_EventId),
    )

    _CS.FSEventStreamCreate.restype = ctypes.c_void_p
    _CS.FSEventStreamCreate.argtypes = [
        ctypes.c_void_p,
        _CALLBACK,
        ctypes.c_void_p,
        ctypes.c_void_p,
        _EventId,
        ctypes.c_double,
        ctypes.c_uint32,
    ]
    _CS.FSEventStreamScheduleWithRunLoop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _CS.FSEventStreamStart.restype = ctypes.c_bool
    _CS.FSEventStreamStart.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamFlushSync.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamStop.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamInvalidate.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamRelease.argtypes = [ctypes.c_void_p]


@dataclass(frozen=True)
class VolumeIdentity:
    device: int
    uuid: Optional[str]

    @property
    def supports_history(self) -> bool:
        return self.uuid is not None


@dataclass(frozen=True)
class HistoryProbe:
    usable: bool
    reason: str = ""


def current_event_id() -> int:
    if not FSEVENTS_AVAILABLE:
        return 0
    return int(_CS.FSEventsGetCurrentEventId())


def _last_event_id_before_time(device: int, when: float) -> int:
    if not FSEVENTS_AVAILABLE:
        return _EVENT_ID_NOT_FOUND
    return int(
        _CS.FSEventsGetLastEventIdForDeviceBeforeTime(
            ctypes.c_int32(device), ctypes.c_double(when)
        )
    )


def volume_identity(path: Path) -> Optional[VolumeIdentity]:
    try:
        device = os.stat(str(path)).st_dev
    except OSError:
        return None
    if not FSEVENTS_AVAILABLE:
        return VolumeIdentity(device=device, uuid=None)

    uuid_ref = _CS.FSEventsCopyUUIDForDevice(ctypes.c_int32(device))
    if not uuid_ref:
        return VolumeIdentity(device=device, uuid=None)
    string_ref = _CF.CFUUIDCreateString(None, uuid_ref)
    try:
        buffer = ctypes.create_string_buffer(64)
        if not _CF.CFStringGetCString(string_ref, buffer, 64, _UTF8):
            return VolumeIdentity(device=device, uuid=None)
        return VolumeIdentity(device=device, uuid=buffer.value.decode("ascii"))
    finally:
        if string_ref:
            _CF.CFRelease(string_ref)
        _CF.CFRelease(uuid_ref)


def probe_history(device: int, since_event_id: int, since_time: float) -> HistoryProbe:
    """Decide whether FSEvents can still explain what changed since our snapshot.

    A spike proved that replaying an ancient `sinceWhen` silently returns a
    truncated history with no mustScanSubDirs warning, so that flag cannot be
    trusted. `FSEventsGetLastEventIdForDeviceBeforeTime` is authoritative: it
    returns 0 when nothing before `since_time` is retained, and an id newer
    than our snapshot when the intervening events were discarded.
    """
    if not FSEVENTS_AVAILABLE:
        return HistoryProbe(usable=False, reason="fsevents-unavailable")
    if since_event_id <= 0:
        return HistoryProbe(usable=False, reason="no-baseline")

    oldest_retained = _last_event_id_before_time(device, since_time)
    if oldest_retained == _EVENT_ID_NOT_FOUND:
        return HistoryProbe(usable=False, reason="no-retained-history")
    if oldest_retained > since_event_id:
        return HistoryProbe(usable=False, reason="history-truncated")
    return HistoryProbe(usable=True)


def replay_changed_paths(
    roots: Sequence[Path],
    since_event_id: int,
    timeout_seconds: float = 10.0,
) -> Optional[Set[str]]:
    """Return every path FSEvents reports as touched since `since_event_id`.

    Returns None when the caller must fall back to a full metadata walk:
    FSEvents unavailable, stream creation failed, the daemon reported dropped
    events, or the history sentinel never arrived within the timeout.
    """
    if not FSEVENTS_AVAILABLE or not roots:
        return None

    changed: Set[str] = set()
    dropped = threading.Event()
    history_done = threading.Event()

    def _on_events(stream, info, count, event_paths, event_flags, event_ids):
        entries = ctypes.cast(event_paths, ctypes.POINTER(ctypes.c_char_p))
        for index in range(count):
            flags = event_flags[index]
            if flags & _FLAG_HISTORY_DONE:
                history_done.set()
                continue
            if flags & DROP_FLAGS:
                dropped.set()
            raw = entries[index]
            if not raw:
                continue
            path = raw.decode("utf-8", "replace")
            changed.add(path)
            if flags & (_FLAG_MUST_SCAN_SUBDIRS | _FLAG_ROOT_CHANGED):
                # Not a gap detector, but when it does arrive the whole
                # subtree below this path must be revisited.
                changed.add(os.path.join(path, "*"))

    callback = _CALLBACK(_on_events)
    string_refs: List[int] = []
    for root in roots:
        ref = _CF.CFStringCreateWithCString(None, str(root).encode("utf-8"), _UTF8)
        if ref:
            string_refs.append(ref)
    if not string_refs:
        return None

    array_items = (ctypes.c_void_p * len(string_refs))(*string_refs)
    paths_array = _CF.CFArrayCreate(
        None, array_items, len(string_refs), ctypes.byref(_ARRAY_CALLBACKS)
    )
    for ref in string_refs:
        _CF.CFRelease(ref)
    if not paths_array:
        return None

    stream = _CS.FSEventStreamCreate(
        None,
        callback,
        None,
        paths_array,
        ctypes.c_uint64(since_event_id),
        ctypes.c_double(0.2),
        _CREATE_FLAG_NO_DEFER | _CREATE_FLAG_FILE_EVENTS | _CREATE_FLAG_WATCH_ROOT,
    )
    _CF.CFRelease(paths_array)
    if not stream:
        return None

    try:
        _CS.FSEventStreamScheduleWithRunLoop(
            stream, _CF.CFRunLoopGetCurrent(), _RUN_LOOP_DEFAULT_MODE
        )
        if not _CS.FSEventStreamStart(stream):
            return None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline and not history_done.is_set():
            _CF.CFRunLoopRunInMode(_RUN_LOOP_DEFAULT_MODE, ctypes.c_double(0.2), True)
        _CS.FSEventStreamFlushSync(stream)
        _CF.CFRunLoopRunInMode(_RUN_LOOP_DEFAULT_MODE, ctypes.c_double(0.3), True)
    finally:
        _CS.FSEventStreamStop(stream)
        _CS.FSEventStreamInvalidate(stream)
        _CS.FSEventStreamRelease(stream)

    if dropped.is_set() or not history_done.is_set():
        return None
    return changed
