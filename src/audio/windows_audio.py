"""Read-only Windows audio endpoint and application-session discovery.

The application deliberately uses Core Audio only to identify endpoint IDs and the
default *communications* endpoint. PortAudio/sounddevice continues to own streams.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import uuid


@dataclass(frozen=True)
class CoreAudioEndpoint:
    endpoint_id: str
    name: str
    direction: str
    default_communications: bool = False


@dataclass(frozen=True)
class CoreAudioSession:
    process_id: int
    application_name: str
    state: str


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> "_GUID":
        raw = uuid.UUID(value).bytes_le
        return cls.from_buffer_copy(raw)


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT_UNION(ctypes.Union):
    _fields_ = [("pwszVal", wintypes.LPWSTR), ("ullVal", ctypes.c_ulonglong)]


class _PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("value", _PROPVARIANT_UNION),
    ]


_CLSID_MMDEVICE_ENUMERATOR = _GUID.from_string("bcde0395-e52f-467c-8e3d-c4579291692e")
_IID_IMMDEVICE_ENUMERATOR = _GUID.from_string("a95664d2-9614-4f35-a746-de8db63617e6")
_IID_IAUDIO_SESSION_MANAGER2 = _GUID.from_string("77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f")
_IID_IAUDIO_SESSION_CONTROL2 = _GUID.from_string("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")
_PKEY_DEVICE_FRIENDLY_NAME = _PROPERTYKEY(
    _GUID.from_string("a45c254e-df1c-4efd-8020-67d146a850e0"), 14
)
_CLSCTX_INPROC_SERVER = 1
_DEVICE_STATE_ACTIVE = 1
_STGM_READ = 0
_VT_LPWSTR = 31
_E_RENDER = 0
_E_CAPTURE = 1
_E_COMMUNICATIONS = 2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def query_core_audio_endpoints() -> list[CoreAudioEndpoint]:
    """Return active render/capture endpoints and communications defaults."""
    if sys.platform != "win32":
        return []
    ole32 = ctypes.windll.ole32
    initialized = ole32.CoInitializeEx(None, 0) >= 0
    enumerator = ctypes.c_void_p()
    try:
        _check_hresult(
            ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MMDEVICE_ENUMERATOR),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_IMMDEVICE_ENUMERATOR),
                ctypes.byref(enumerator),
            )
        )
        results: list[CoreAudioEndpoint] = []
        for flow, direction in ((_E_CAPTURE, "input"), (_E_RENDER, "output")):
            default_id = _default_endpoint_id(enumerator, flow)
            for endpoint in _enumerate_flow(enumerator, flow):
                results.append(
                    CoreAudioEndpoint(
                        endpoint_id=endpoint[0],
                        name=endpoint[1],
                        direction=direction,
                        default_communications=endpoint[0] == default_id,
                    )
                )
        return results
    finally:
        if enumerator.value:
            _release(enumerator)
        if initialized:
            ole32.CoUninitialize()


def query_audio_sessions(endpoint_id: str) -> list[CoreAudioSession]:
    """Return active and retained audio sessions for one endpoint."""
    if sys.platform != "win32":
        return []
    ole32 = ctypes.windll.ole32
    initialized = ole32.CoInitializeEx(None, 0) >= 0
    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    manager = ctypes.c_void_p()
    session_enumerator = ctypes.c_void_p()
    try:
        _check_hresult(
            ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MMDEVICE_ENUMERATOR), None, _CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_IMMDEVICE_ENUMERATOR), ctypes.byref(enumerator),
            )
        )
        _check_hresult(
            _call(enumerator, 5, ctypes.c_long, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))(
                enumerator, endpoint_id, ctypes.byref(device)
            )
        )
        _check_hresult(
            _call(
                device, 3, ctypes.c_long, ctypes.POINTER(_GUID), wintypes.DWORD,
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            )(
                device, ctypes.byref(_IID_IAUDIO_SESSION_MANAGER2),
                _CLSCTX_INPROC_SERVER, None, ctypes.byref(manager),
            )
        )
        _check_hresult(
            _call(manager, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                manager, ctypes.byref(session_enumerator)
            )
        )
        count = ctypes.c_int()
        _check_hresult(
            _call(session_enumerator, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                session_enumerator, ctypes.byref(count)
            )
        )
        sessions: list[CoreAudioSession] = []
        for index in range(count.value):
            control = ctypes.c_void_p()
            control2 = ctypes.c_void_p()
            try:
                _check_hresult(
                    _call(
                        session_enumerator, 4, ctypes.c_long, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_void_p),
                    )(session_enumerator, index, ctypes.byref(control))
                )
                _check_hresult(
                    _call(
                        control, 0, ctypes.c_long, ctypes.POINTER(_GUID),
                        ctypes.POINTER(ctypes.c_void_p),
                    )(control, ctypes.byref(_IID_IAUDIO_SESSION_CONTROL2), ctypes.byref(control2))
                )
                state = ctypes.c_int()
                process_id = wintypes.DWORD()
                _check_hresult(
                    _call(control2, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                        control2, ctypes.byref(state)
                    )
                )
                if state.value == 2:
                    continue
                _check_hresult(
                    _call(control2, 14, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(
                        control2, ctypes.byref(process_id)
                    )
                )
                display_name = _session_display_name(control2)
                process_name = _process_name(process_id.value)
                sessions.append(
                    CoreAudioSession(
                        process_id=process_id.value,
                        application_name=(
                            ("Sonidos del sistema" if process_id.value == 0 else "")
                            or process_name
                            or display_name
                            or f"Proceso {process_id.value}"
                        ),
                        state="active" if state.value == 1 else "inactive",
                    )
                )
            except OSError:
                continue
            finally:
                if control2.value:
                    _release(control2)
                if control.value:
                    _release(control)
        unique = {(item.process_id, item.application_name, item.state): item for item in sessions}
        return sorted(unique.values(), key=lambda item: (item.state != "active", item.application_name.casefold()))
    finally:
        for pointer in (session_enumerator, manager, device, enumerator):
            if pointer.value:
                _release(pointer)
        if initialized:
            ole32.CoUninitialize()


def _enumerate_flow(enumerator: ctypes.c_void_p, flow: int) -> list[tuple[str, str]]:
    collection = ctypes.c_void_p()
    _check_hresult(_call(enumerator, 3, ctypes.c_long, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(enumerator, flow, _DEVICE_STATE_ACTIVE, ctypes.byref(collection)))
    try:
        count = wintypes.UINT()
        _check_hresult(_call(collection, 3, ctypes.c_long, ctypes.POINTER(wintypes.UINT))(collection, ctypes.byref(count)))
        result = []
        for index in range(count.value):
            device = ctypes.c_void_p()
            _check_hresult(_call(collection, 4, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(collection, index, ctypes.byref(device)))
            try:
                result.append((_device_id(device), _friendly_name(device)))
            finally:
                _release(device)
        return result
    finally:
        _release(collection)


def _default_endpoint_id(enumerator: ctypes.c_void_p, flow: int) -> str | None:
    device = ctypes.c_void_p()
    hr = _call(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(enumerator, flow, _E_COMMUNICATIONS, ctypes.byref(device))
    if hr < 0:
        return None
    try:
        return _device_id(device)
    finally:
        _release(device)


def _device_id(device: ctypes.c_void_p) -> str:
    value = wintypes.LPWSTR()
    _check_hresult(_call(device, 5, ctypes.c_long, ctypes.POINTER(wintypes.LPWSTR))(device, ctypes.byref(value)))
    try:
        return value.value or ""
    finally:
        ctypes.windll.ole32.CoTaskMemFree(value)


def _friendly_name(device: ctypes.c_void_p) -> str:
    store = ctypes.c_void_p()
    _check_hresult(_call(device, 4, ctypes.c_long, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(device, _STGM_READ, ctypes.byref(store)))
    value = _PROPVARIANT()
    try:
        _check_hresult(_call(store, 5, ctypes.c_long, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))(store, ctypes.byref(_PKEY_DEVICE_FRIENDLY_NAME), ctypes.byref(value)))
        return value.pwszVal or "" if value.vt == _VT_LPWSTR else ""
    finally:
        ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
        _release(store)


def _session_display_name(control: ctypes.c_void_p) -> str:
    value = wintypes.LPWSTR()
    hr = _call(control, 4, ctypes.c_long, ctypes.POINTER(wintypes.LPWSTR))(
        control, ctypes.byref(value)
    )
    if hr < 0:
        return ""
    try:
        return value.value or ""
    finally:
        if value:
            ctypes.windll.ole32.CoTaskMemFree(value)


def _process_name(process_id: int) -> str:
    if not process_id:
        return ""
    kernel32 = ctypes.windll.kernel32
    process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not process:
        return ""
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(capacity)):
            return ""
        return Path(buffer.value).stem
    finally:
        kernel32.CloseHandle(process)


def _call(pointer: ctypes.c_void_p, index: int, result: object, *arguments: object):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(vtable[index])


def _release(pointer: ctypes.c_void_p) -> None:
    _call(pointer, 2, wintypes.ULONG)(pointer)


def _check_hresult(value: int) -> None:
    if value < 0:
        raise OSError(f"Windows Core Audio call failed (HRESULT 0x{value & 0xffffffff:08X}).")
