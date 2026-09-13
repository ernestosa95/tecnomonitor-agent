"""
Monitoreo de equipos Linux vía SSH (ver docs/PLAN_MEJORAS_V4.5.md §9.2) y el
despacho WMI/SSH de obtener_vm_data.

Mockea paramiko.SSHClient a nivel de objeto (connect/exec_command), no el
protocolo SSH en sí — eso es responsabilidad de paramiko, una librería
madura. Lo que hay que verificar es la lógica propia: parseo de /proc/*, df,
systemctl, ps, y el mapeo al mismo vm_obj que produce el camino WMI.
"""
from unittest import mock

import paramiko
import pytest

import agent_logic


class _Stream:
    def __init__(self, texto):
        self._bytes = texto.encode("utf-8")

    def read(self):
        return self._bytes


RESPUESTAS = {
    "hostname": "linux-elk-01\n",
    "awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t, a}' /proc/meminfo": "16384000 8192000\n",
    "cat /proc/uptime": "123456.78 98765.43\n",
}

DF_OUTPUT = (
    "/dev/sda1 21474836480 8589934592 12884901888 55% /\n"
    "/dev/sda2 107374182400 32212254720 75161927680 30% /var/lib/elasticsearch\n"
)

SYSTEMCTL_OUTPUTS = {
    "elasticsearch": "active\nrunning\n4321\n",
    "logstash":      "inactive\ndead\n0\n",
}


def _make_fake_exec_command(cpu_stat_muestras):
    def _fake_exec_command(self, comando, timeout=None):
        if comando in RESPUESTAS:
            salida = RESPUESTAS[comando]
        elif comando == "grep '^cpu ' /proc/stat":
            salida = next(cpu_stat_muestras, "cpu  0 0 0 0 0 0 0 0 0 0\n")
        elif comando.startswith("df -P -B1"):
            salida = DF_OUTPUT
        elif comando.startswith("systemctl show"):
            salida = ""
            for unidad, resp in SYSTEMCTL_OUTPUTS.items():
                if unidad in comando:
                    salida = resp
                    break
        elif comando.startswith("ps -o %cpu,rss,nlwp"):
            salida = "  2.5 512000    12\n"
        elif "wc -l" in comando and "/fd" in comando:
            salida = "37\n"
        else:
            raise AssertionError(f"comando no esperado por el mock: {comando!r}")
        return None, _Stream(salida), _Stream("")
    return _fake_exec_command


@pytest.fixture()
def vm_info_linux():
    return {"nombre": "", "type": "eq", "os": "linux", "ip": "10.0.0.99",
            "user": "monitor", "pass": "cualquiera", "servicios": "elasticsearch,logstash"}


@pytest.fixture()
def ssh_mockeado():
    # total 1000->2000 (delta 1000), idle 850->1700 (delta 850) => uso = 15.0%
    muestras = iter(["cpu  100 0 50 850 0 0 0 0 0 0\n", "cpu  200 0 100 1700 0 0 0 0 0 0\n"])
    with mock.patch.object(agent_logic, "verificar_puerto", return_value=True), \
         mock.patch.object(paramiko.SSHClient, "connect", lambda self, *a, **k: None), \
         mock.patch.object(paramiko.SSHClient, "exec_command", _make_fake_exec_command(muestras)), \
         mock.patch.object(paramiko.SSHClient, "close", lambda self: None):
        yield


def test_connection_vm_ssh_detecta_hostname_real(vm_info_linux, ssh_mockeado):
    res = agent_logic.test_connection_vm_ssh(vm_info_linux)
    assert res["success"] is True
    assert res["hostname"] == "linux-elk-01"


def test_recoleccion_ssh_completa_produce_el_mismo_vm_obj_que_wmi(vm_info_linux, ssh_mockeado):
    vm_obj = agent_logic.obtener_vm_data((vm_info_linux, None))

    assert vm_obj["state"] == "Online"
    assert vm_obj["state_reason"] == "ok"
    assert vm_obj["os"] == "linux"
    assert vm_obj["id"] == "linux-elk-01", "sin nombre manual, usa el hostname real detectado"

    tel = vm_obj["telemetry"]
    assert tel["ram"]["total_gb"] == round(16384000 / 1048576, 2)
    assert tel["ram"]["used_gb"] == round((16384000 - 8192000) / 1048576, 2)
    assert tel["uptime_seconds"] == 123456
    assert tel["cpu"]["usage_percent"] == 15.0, "delta de dos muestras de /proc/stat: (1 - 850/1000) * 100"

    assert len(vm_obj["storage"]) == 2
    raiz = next(s for s in vm_obj["storage"] if s["mount_point"] == "/")
    assert raiz["total_gb"] == round(21474836480 / 1073741824, 2)
    assert "performance" not in raiz, "no hay latencia de disco confiable en Linux sin mapear a device real (LVM/RAID)"

    servicios = {s["name"]: s for s in vm_obj["application_layer"]["services"]}
    assert servicios["elasticsearch"]["state"] == "Running"
    vs = servicios["elasticsearch"]["vital_signs"]
    assert vs["pid"] == 4321
    assert vs["cpu_percent"] == 2.5
    assert vs["ram_mb"] == round(512000 / 1024, 1)
    assert vs["threads"] == 12
    assert vs["handles"] == 37, "en Linux 'handles' son file descriptors abiertos, no el mismo concepto que en Windows"
    assert servicios["logstash"]["state"] != "Running"


def test_falla_de_conexion_ssh_usa_collection_error_no_wmi_error(vm_info_linux):
    with mock.patch.object(agent_logic, "verificar_puerto", return_value=True), \
         mock.patch.object(paramiko.SSHClient, "connect",
                            side_effect=paramiko.AuthenticationException("credenciales rechazadas")):
        vm_obj = agent_logic._recolectar_ssh_interno(dict(vm_info_linux), None)

    assert vm_obj["state"] == "Offline"
    assert vm_obj["state_reason"] == "ssh_error"
    assert "collection_error" in vm_obj
    assert "wmi_error" not in vm_obj, "el campo de error es genérico desde que hay dos mecanismos de recolección"


def test_puerto_22_cerrado_no_intenta_conectar(vm_info_linux):
    with mock.patch.object(agent_logic, "verificar_puerto", return_value=False):
        vm_obj = agent_logic._recolectar_ssh_interno(dict(vm_info_linux), None)
    assert vm_obj["state_reason"] == "port_closed"


def test_obtener_vm_data_sin_campo_os_sigue_yendo_por_wmi_retrocompatible():
    with mock.patch.object(agent_logic, "_recolectar_wmi_interno", return_value={"id": "x"}) as wmi_mock, \
         mock.patch.object(agent_logic, "_recolectar_ssh_interno") as ssh_mock:
        agent_logic.obtener_vm_data(({"ip": "1.2.3.4"}, None))

    assert wmi_mock.called
    assert not ssh_mock.called, "una config vieja sin 'os' debe seguir usando WMI, no romperse ni cambiar de camino"
