"""The DeathAdder 3.5G through libusb-win32's filter driver (issue #29)."""
import hashlib
import io
import json
import os
import zipfile

import pytest

from openrazer_win import usbfilter
from openrazer_win.core.deathadder35g import DeathAdder35GDevice
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.persistence import Persistence
from openrazer_win.hid.fake import FakeHidBackend, FakeLibusb, FakeRazerDevice

PID = 0x0016


@pytest.fixture
def persistence(tmp_path):
    return Persistence(str(tmp_path / 'state.json'))


def _manager(persistence, filtered=(PID,), pid=PID):
    backend = FakeHidBackend([FakeRazerDevice(pid, 'Razer DeathAdder 3.5G')],
                             libusb=FakeLibusb(filtered))
    return DeviceManager(backend=backend, persistence=persistence, bluetooth=False), backend


def test_the_filter_makes_it_a_device(persistence):
    manager, backend = _manager(persistence)
    devices = manager.scan()
    assert len(devices) == 1 and isinstance(devices[0], DeathAdder35GDevice)
    assert manager.unreachable == []
    caps = devices[0].capabilities()
    assert caps['dpi_choices'] == [450, 900, 1800, 3500]
    assert caps['supported_poll_rates'] == [125, 500, 1000]
    assert set(caps['zones']) == {'logo', 'scroll'}
    assert caps['transport']['path'] == 'libusb-win32'


def test_without_the_filter_it_stays_out_of_reach(persistence):
    manager, _ = _manager(persistence, filtered=())
    assert manager.scan() == []
    assert manager.unreachable == [(PID, 'Razer DeathAdder 3.5G')]


def test_it_sends_upstreams_four_bytes(persistence):
    manager, backend = _manager(persistence)
    device = manager.scan()[0]
    device.restore()
    # SET_REPORT, report type 0: poll 1000, 3500 DPI, profile 1, both lights.
    assert backend.libusb.sent[-1] == (PID, 0x21, 0x09, 0x0010, 0x0000, bytes((1, 1, 1, 3)))
    device.set_dpi(1700)
    assert backend.libusb.sent[-1][-1] == bytes((1, 2, 1, 3))
    device.set_poll_rate(500)
    assert backend.libusb.sent[-1][-1] == bytes((2, 2, 1, 3))
    device.set_none('logo')
    assert backend.libusb.sent[-1][-1] == bytes((2, 2, 1, 2))
    device.set_none('scroll')
    assert backend.libusb.sent[-1][-1] == bytes((2, 2, 1, 0))
    assert device.get_dpi() == (1800, 1800) and device.get_poll_rate() == 500


def test_its_state_comes_back_when_it_is_plugged_in_again(persistence):
    manager, backend = _manager(persistence)
    device = manager.scan()[0]
    device.set_dpi(450)
    device.set_none('scroll')
    again = DeathAdder35GDevice(device.info, backend.libusb, persistence)
    again.restore()
    assert backend.libusb.sent[-1][-1] == bytes((1, 4, 1, 1))


def test_a_failing_library_is_a_device_error(persistence):
    from openrazer_win.core.device import DeviceError
    manager, backend = _manager(persistence)
    device = manager.scan()[0]
    backend.libusb.fail = True
    with pytest.raises(DeviceError):
        device.set_dpi(900)


def test_the_black_edition_has_no_lights(persistence):
    manager, _ = _manager(persistence, filtered=(0x0029,), pid=0x0029)
    device = manager.scan()[0]
    assert device.capabilities()['zones'] == {}
    from openrazer_win.devices.recipes import NotSupported
    with pytest.raises(NotSupported):
        device.set_on('logo')


def test_studio_reaches_it_over_rpc(tmp_path):
    from openrazer_win.daemon.demo import build_demo_backend
    from openrazer_win.daemon.server import DaemonService
    service = DaemonService(backend=build_demo_backend((PID,), filtered=(PID,)),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False)
    service.start()
    try:
        serial = service.rpc_list_devices()[0]['serial']
        service.rpc_call(serial, 'set_dpi', [900, 900])
        assert service.rpc_call(serial, 'get_dpi') == [900, 900] or \
            tuple(service.rpc_call(serial, 'get_dpi')) == (900, 900)
        assert service.rpc_capabilities(serial)['dpi_choices'] == [450, 900, 1800, 3500]
        assert service.rpc_unsupported() == []
    finally:
        service.stop()


# -- installing the driver ---------------------------------------------------------
def _archive(arch='amd64'):
    contents = {name: ('fake ' + name).encode() for name in usbfilter.FILES[arch]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as bundle:
        for name, data in contents.items():
            bundle.writestr('libusb-win32-bin-{0}/bin/{1}/{2}'.format(usbfilter.VERSION, arch, name), data)
    data = buffer.getvalue()
    digests = {name: hashlib.sha256(blob).hexdigest() for name, blob in contents.items()}
    return data, hashlib.sha256(data).hexdigest(), digests


class _Ran:
    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, args, **kwargs):
        self.calls.append(args)

        class Result:
            returncode = self.code
            stdout = 'done' if self.code == 0 else 'install-filter: failed'
            stderr = ''
        return Result()


def test_install_checks_copies_and_filters_only_the_mouse(tmp_path, monkeypatch):
    data, digest, digests = _archive()
    monkeypatch.setattr(usbfilter, 'ZIP_SHA256', digest)
    monkeypatch.setitem(usbfilter.FILES, 'amd64', digests)
    monkeypatch.setattr(usbfilter, 'architecture', lambda: 'amd64')
    system = tmp_path / 'System32'
    (system / 'drivers').mkdir(parents=True)
    # Another program's libusb0.dll stays as it is.
    (system / 'libusb0.dll').write_bytes(b'someone else')
    ran = _Ran()
    done = usbfilter.install([PID, PID], fetch=lambda url: data, run=ran,
                             home=str(tmp_path / 'home'), system_dir=str(system))
    assert (system / 'drivers' / 'libusb0.sys').read_bytes() == b'fake libusb0.sys'
    assert (system / 'libusb0.dll').read_bytes() == b'someone else'
    assert ran.calls == [[str(tmp_path / 'home' / 'install-filter.exe'), 'install',
                          '--device=USB\\VID_1532&PID_0016']]
    assert done == ['copied libusb0.sys', 'filter on USB\\VID_1532&PID_0016']


def test_a_tampered_download_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(usbfilter, 'architecture', lambda: 'amd64')
    ran = _Ran()
    with pytest.raises(usbfilter.DriverError, match='not the one'):
        usbfilter.install([PID], fetch=lambda url: b'not the release', run=ran,
                          home=str(tmp_path), system_dir=str(tmp_path))
    assert ran.calls == []


def test_a_tampered_file_inside_is_refused(tmp_path, monkeypatch):
    data, digest, digests = _archive()
    digests['libusb0.sys'] = '0' * 64
    monkeypatch.setattr(usbfilter, 'ZIP_SHA256', digest)
    monkeypatch.setitem(usbfilter.FILES, 'amd64', digests)
    monkeypatch.setattr(usbfilter, 'architecture', lambda: 'amd64')
    with pytest.raises(usbfilter.DriverError, match='libusb0.sys'):
        usbfilter.install([PID], fetch=lambda url: data, run=_Ran(),
                          home=str(tmp_path / 'h'), system_dir=str(tmp_path))


def test_a_failed_filter_tool_says_so(tmp_path, monkeypatch):
    data, digest, digests = _archive()
    monkeypatch.setattr(usbfilter, 'ZIP_SHA256', digest)
    monkeypatch.setitem(usbfilter.FILES, 'amd64', digests)
    monkeypatch.setattr(usbfilter, 'architecture', lambda: 'amd64')
    (tmp_path / 'drivers').mkdir()
    with pytest.raises(usbfilter.DriverError, match='install-filter install failed'):
        usbfilter.install([PID], fetch=lambda url: data, run=_Ran(code=1),
                          home=str(tmp_path / 'h'), system_dir=str(tmp_path))


def test_remove_takes_the_filter_off(tmp_path, monkeypatch):
    data, digest, digests = _archive()
    monkeypatch.setitem(usbfilter.FILES, 'amd64', digests)
    monkeypatch.setattr(usbfilter, 'architecture', lambda: 'amd64')
    usbfilter.unpack(data, str(tmp_path), 'amd64')
    ran = _Ran()
    assert usbfilter.remove([PID], run=ran, home=str(tmp_path)) == [
        'filter off USB\\VID_1532&PID_0016']
    assert ran.calls[0][1:] == ['uninstall', '--device=USB\\VID_1532&PID_0016']


def test_the_pinned_hashes_are_the_real_release():
    # Taken from the release on 2026-11-24; GitHub lists the same digest.
    assert usbfilter.ZIP_SHA256 == '00004c92cdb99be36e17fb2377165eb97e63b48ba895bfc04a642ea9c3e26d94'
    assert usbfilter.ZIP_URL.endswith('/release_1.4.0.2/libusb-win32-bin-1.4.0.2.zip')
    assert all(len(d) == 64 for files in usbfilter.FILES.values() for d in files.values())


# -- the command line --------------------------------------------------------------
def test_the_cli_hands_install_to_an_administrator_run(tmp_path, monkeypatch, capsys):
    from openrazer_win import cli, elevate

    monkeypatch.setattr(usbfilter, 'is_admin', lambda: False)
    seen = {}

    def fake_elevated(args, command=None):
        seen['args'] = args
        result = args[args.index('--result') + 1]
        with open(result, 'w', encoding='utf-8') as handle:
            json.dump({'ok': True, 'done': ['filter on USB\\VID_1532&PID_0016']}, handle)
        return 0

    monkeypatch.setattr(elevate, 'run_elevated', fake_elevated)
    monkeypatch.setattr('openrazer_win.cli.RpcClient', lambda **kw: (_ for _ in ()).throw(OSError()))
    assert cli.main(['driver', 'install', '--pid', '0016', '--json']) == 0
    assert seen['args'][:2] == ['driver', 'install'] and seen['args'][-2:] == ['--pid', '0016']
    assert json.loads(capsys.readouterr().out)['ok'] is True
    assert not os.path.exists(seen['args'][3])


def test_the_cli_reports_a_declined_prompt(monkeypatch, capsys):
    from openrazer_win import cli, elevate

    monkeypatch.setattr(usbfilter, 'is_admin', lambda: False)

    def declined(args, command=None):
        raise elevate.Declined('the administrator prompt was declined')

    monkeypatch.setattr(elevate, 'run_elevated', declined)
    assert cli.main(['driver', 'install', '--pid', '0016']) == 2
    assert 'declined' in capsys.readouterr().err


def test_the_cli_refuses_other_devices():
    from openrazer_win import cli
    assert cli.main(['driver', 'install', '--pid', '0078']) == 2
