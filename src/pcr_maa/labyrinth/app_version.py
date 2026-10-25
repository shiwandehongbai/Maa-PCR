"""Read APP-VER from the selected game's already owned device."""
import re
import time

from .batch import BatchStopped
from .servers import get_server

PACKAGE = 'com.bilibili.priconne'
COMMAND = 'dumpsys package ' + PACKAGE


def validate_version(value):
    if type(value) is not str or re.fullmatch(r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}', value) is None:
        raise ValueError('game_version_invalid')
    return value


def parse_package_version(output, *, server='cn-bilibili'):
    package=get_server(server).package
    if type(output) is not str or len(output) > 1024 * 1024 or '\x00' in output:
        raise ValueError('game_version_invalid')
    packages = list(re.finditer(r'^\s*Package \[([^\]\r\n]+)\][^\r\n]*:', output, re.MULTILINE))
    matches = [(index, match) for index, match in enumerate(packages) if match[1] == package]
    if len(matches) != 1:
        raise ValueError('game_version_invalid')
    index, match = matches[0]
    block = output[match.end():packages[index+1].start() if index+1<len(packages) else len(output)]
    versions = re.findall(r'^[ \t]*versionName=([^\r\n]*)\r?$', block, re.MULTILINE)
    if len(versions) != 1:
        raise ValueError('game_version_invalid')
    return validate_version(versions[0].strip(' \t'))


def read_installed_version(controller, *, stopped, server='cn-bilibili'):
    # Use this Owner's controller, not a second ADB connection or a config value.
    package=get_server(server).package
    if stopped():
        raise BatchStopped('stopped')
    try:
        output=controller_shell(controller,'dumpsys package '+package,stopped=stopped)
        version = parse_package_version(output,server=server)
        if stopped():
            raise BatchStopped('stopped')
        return version
    except BatchStopped:
        raise
    except Exception:
        raise BatchStopped('game_version_unavailable') from None


def controller_shell(controller, command, *, stopped):
    """Bounded use of this owner controller; callers supply fixed commands."""
    if stopped():raise BatchStopped('stopped')
    # shortcut: maafw 5.13 omits these signatures; remove after the pinned SDK fixes them.
    import ctypes
    from maa.define import MaaControllerHandle, MaaStringBufferHandle, MaaId, MaaBool
    from maa.library import Library
    from .. import native
    api=Library.framework()
    api.MaaControllerPostShell.argtypes=[MaaControllerHandle,ctypes.c_char_p,ctypes.c_int64]
    api.MaaControllerPostShell.restype=MaaId
    api.MaaControllerGetShellOutput.argtypes=[MaaControllerHandle,MaaStringBufferHandle]
    api.MaaControllerGetShellOutput.restype=MaaBool
    if native.active_now()>=native.RUN_DEADLINE.get():raise TimeoutError('controller_shell_timeout')
    job=controller.post_shell(command,timeout=5000)
    deadline=min(native.active_now()+6,native.RUN_DEADLINE.get())
    while not job.done:
        if stopped():raise BatchStopped('stopped')
        if native.active_now()>=deadline:raise TimeoutError('controller_shell_timeout')
        time.sleep(.05)
    if stopped():raise BatchStopped('stopped')
    if native.active_now()>=deadline or not job.succeeded:raise ValueError('controller_shell_failed')
    output=job.get()
    if stopped():raise BatchStopped('stopped')
    if type(output) is not str or len(output)>2*1024*1024 or '\x00' in output:
        raise ValueError('controller_shell_invalid')
    return output
