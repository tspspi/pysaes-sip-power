"""Exercise generic channel dispatch with a Gamma QPC-shaped backend."""

import pytest
from labdevices.ionpump import IonPump
from labdevices.pressuregauge import PressureGaugeUnit


class QuadPump(IonPump):
    def __init__(self):
        super().__init__(nChannels = 4, defaultChannel = 2)
        self.calls = []
        self.connected = False
    def _connect(self):
        self.connected = True
        return True
    def _disconnect(self):
        self.connected = False
        self.calls.append(('disconnect',))
        return True
    def _is_connected(self):
        return self.connected
    def _start(self, channel):
        self.calls.append(('enable', channel))
    def _stop(self, channel):
        self.calls.append(('disable', channel))
    def _get_voltage(self, channel):
        self.calls.append(('getVoltage', channel))
        return 5000
    def _get_current(self, channel):
        self.calls.append(('getCurrent', channel))
        return 1e-6
    def _get_pump_size(self, channel):
        self.calls.append(('getPumpSize', channel))
        return 150
    def _get_measurement(self, channel):
        return {'channel': channel, 'pressure_mbar': channel * 1e-7}


def test_quad_controller_default_and_explicit_channels():
    with QuadPump() as pump:
        assert pump.get_channel_count() == 4
        pump.start()
        pump.stop(4)
        pump.get_voltage(3)
        pump.get_current(1)
        assert pump.get_pump_size(4) == 150
        assert pump.get_pressure(channel = 4) == 4e-7
        assert pump.get_pressure(PressureGaugeUnit.PASCAL, channel = 4) == pytest.approx(4e-5)
        pump.set_default_channel(3)
        assert pump.get_default_channel() == 3
        pump.start()
        for channel in (0, 5, True, 1.5, '1'):
            with pytest.raises(ValueError):
                pump.stop(channel)
        assert pump.calls == [('enable', 2), ('disable', 4), ('getVoltage', 3),
            ('getCurrent', 1), ('getPumpSize', 4), ('enable', 3)]
        with pytest.raises(NotImplementedError):
            pump.restart(1)
    assert pump.calls[-1] == ('disconnect',)
    assert not pump.is_connected()


def test_connection_lifecycle_and_no_implicit_stop():
    pump = QuadPump()
    pump.connect()
    with pytest.raises(ValueError):
        pump.__enter__()
    pump.disconnect()
    with pump:
        with pytest.raises(ValueError):
            pump.connect()
        with pytest.raises(ValueError):
            pump.disconnect()
    assert all(call[0] == 'disconnect' for call in pump.calls)


@pytest.mark.parametrize('channels,default', [(0, 1), (True, 1), (4, 5), (4, True)])
def test_invalid_channel_configuration(channels, default):
    with pytest.raises(ValueError):
        IonPump(nChannels = channels, defaultChannel = default)
