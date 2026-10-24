import json
import os
import re
import socket
import struct
import urllib.parse
import urllib.request

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto.database.svc.cluster.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
HOSTS = {"nfs.dom.lan": "BCK1", "nfs2.dom.lan": "BCK2", "qnap.dom.lan": "QNAP"}
LIFE_LEFT_IDS = ["177", "233", "173", "231"]
RANK = {"OK": 0, "Watch": 1, "Replace": 2}


def query(expr):
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=30) as r:
        return [(s["metric"], float(s["value"][1])) for s in json.load(r)["data"]["result"]]


def by(expr, *labels):
    return {tuple(m.get(lbl, "") for lbl in labels): v for m, v in query(expr)}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def judge(disk):
    replace, watch = [], []
    if disk.get("smart_ok") == 0:
        replace.append("SMART failed")
    if disk.get("health") is not None and disk["health"] >= 3:
        replace.append("health critical")
    if disk.get("health") == 2:
        watch.append("health warning")
    if disk.get("status_ok") == 0:
        replace.append("status not OK")
    if disk.get("critical_warning"):
        replace.append("NVMe critical warning")
    used = disk.get("life_used")
    if used is not None:
        if used >= 90:
            replace.append(f"wear {used:.0f}%")
        elif used >= 80:
            watch.append(f"wear {used:.0f}%")
    for key, label in (("pending", "pending sectors"), ("uncorrectable", "uncorrectable errors"), ("media_errors", "media errors")):
        if disk.get(key):
            replace.append(f"{label} {disk[key]:.0f}")
    for key, label in (("reallocated", "reallocated sectors"), ("bad_sectors", "bad sectors")):
        if disk.get(key):
            watch.append(f"{label} {disk[key]:.0f}")
    temp = disk.get("temperature")
    if temp is not None and temp > 55:
        watch.append(f"temperature {temp:.0f} C")
    status = "Replace" if replace else "Watch" if watch else "OK"
    disk["status"] = status
    disk["reasons"] = ", ".join(replace + watch) or "none"
    return disk


def node_disks():
    info = {(m["node"], m["device"]): m for m, _ in query('smartctl_device{node!=""}')}
    smart = by('smartctl_device_smart_status{node!=""}', "node", "device")
    crit = by('smartctl_device_critical_warning{node!=""}', "node", "device")
    used = by('smartctl_device_percentage_used{node!=""}', "node", "device")
    temp = by('smartctl_device_temperature{node!="",temperature_type="current"}', "node", "device")
    hours = by('smartctl_device_power_on_seconds{node!=""} / 3600', "node", "device")
    media = by('smartctl_device_media_errors{node!=""}', "node", "device")
    attrs = {}
    for m, v in query('smartctl_device_attribute{node!="",attribute_id=~"5|173|177|187|197|198|231|233"}'):
        attrs[(m["node"], m["device"], m["attribute_id"], m["attribute_value_type"])] = v
    disks = []
    for (node, dev), m in info.items():
        k = (node, dev)
        life_left = next((attrs[(node, dev, i, "value")] for i in LIFE_LEFT_IDS if (node, dev, i, "value") in attrs), None)
        life_used = used.get(k) if k in used else (100 - life_left if life_left is not None else None)
        disks.append(judge({
            "key": slug(m.get("serial_number") or f"{node}_{dev}"),
            "host": node,
            "group": "Kubernetes",
            "disk": dev,
            "model": m.get("model_name", ""),
            "serial": m.get("serial_number", ""),
            "type": "NVMe" if m.get("protocol") == "NVMe" else "SSD",
            "smart_ok": smart.get(k),
            "critical_warning": crit.get(k),
            "life_used": life_used,
            "temperature": temp.get(k),
            "power_on_hours": hours.get(k),
            "reallocated": attrs.get((node, dev, "5", "raw")),
            "pending": attrs.get((node, dev, "197", "raw")),
            "uncorrectable": attrs.get((node, dev, "187", "raw")) or attrs.get((node, dev, "198", "raw")),
            "media_errors": media.get(k),
        }))
    return disks


def synology_disks():
    models = {(m["instance"], m["diskID"]): m["diskModel"] for m, _ in query("diskModel")}
    health = by("diskHealthStatus", "instance", "diskID")
    status = by("diskStatus", "instance", "diskID")
    bad = by("diskBadSector", "instance", "diskID")
    life = by("diskRemainLife", "instance", "diskID")
    temp = by("diskTemperature", "instance", "diskID")
    retry = by("diskRetry", "instance", "diskID")
    disks = []
    for (inst, did), model in models.items():
        k = (inst, did)
        host = HOSTS.get(inst, inst)
        remain = life.get(k)
        disks.append(judge({
            "key": slug(f"{host}_{did}"),
            "host": host,
            "group": host,
            "disk": did,
            "model": model,
            "serial": "",
            "type": "HDD" if remain is None or remain < 0 else "SSD",
            "health": health.get(k),
            "status_ok": 0 if status.get(k) == 5 else 1,
            "life_used": 100 - remain if remain is not None and remain >= 0 else None,
            "temperature": temp.get(k),
            "bad_sectors": bad.get(k),
            "retries": retry.get(k),
        }))
    return disks


def qnap_disks():
    disks = []
    temps = by("qnapDiskTemperature", "instance", "diskSlot")
    for m, v in query("qnapDiskStatus"):
        host = HOSTS.get(m["instance"], m["instance"])
        model = re.sub(r"\s*\((SATA|SAS|NVMe)\)$", "", m.get("diskModel", ""))
        disks.append(judge({
            "key": slug(m.get("diskSerial") or f"{host}_slot_{m['diskSlot']}"),
            "host": host,
            "group": host,
            "disk": f"Slot {m['diskSlot']}",
            "index": int(m["diskIndex"]),
            "model": model,
            "serial": m.get("diskSerial", ""),
            "type": "NVMe" if "PC711" in model or "nvme" in model.lower() else "HDD",
            "status_ok": v,
            "temperature": temps.get((m["instance"], m["diskSlot"])),
        }))
    smart, nvme, first_row = {}, {}, {}
    for m, v in query("qnapSmartAttrRaw"):
        dev = m["device"]
        if m.get("smartIndex"):
            first_row[dev] = min(first_row.get(dev, 1e9), int(m["smartIndex"]))
        if dev.startswith("/dev/nvme"):
            nvme.setdefault(dev, {})[m["attribute"]] = v
        else:
            smart.setdefault(dev, {})[m["attributeId"]] = v
    order = sorted(first_row, key=first_row.get)
    slots = sorted(disks, key=lambda d: d["index"])
    if len(order) != len(slots) or any(dev.startswith("/dev/nvme") != (d["type"] == "NVMe") for dev, d in zip(order, slots)):
        order = []
        nvme_slots = [d for d in slots if d["type"] == "NVMe"]
        if len(nvme) == 1 and len(nvme_slots) == 1:
            order, slots = list(nvme), nvme_slots
    for dev, d in zip(order, slots):
        d["device"] = dev.removeprefix("/dev/")
        if dev in nvme:
            d["life_used"] = nvme[dev].get("Percentage Used")
            d["media_errors"] = nvme[dev].get("Media and Data Integrity Errors")
        else:
            a = smart.pop(dev, {})
            d["reallocated"] = a.get("5")
            d["pending"] = a.get("197")
            d["uncorrectable"] = a.get("198")
        judge(d)
    notes = []
    for dev, a in sorted(smart.items()):
        parts = []
        if a.get("5"):
            parts.append(f"{a['5']:.0f} reallocated")
        if a.get("197"):
            parts.append(f"{a['197']:.0f} pending")
        if a.get("198"):
            parts.append(f"{a['198']:.0f} uncorrectable")
        if parts:
            notes.append(f"{dev.removeprefix('/dev/')}: " + ", ".join(parts))
    return disks, notes


def storage_states():
    states = []
    for m, v in query("raidStatus"):
        states.append({"host": HOSTS.get(m["instance"], m["instance"]), "name": m["raidName"], "ok": v == 1})
    for m, v in query("qnapPoolStatus"):
        states.append({"host": "QNAP", "name": f"Pool {m['poolName']}", "ok": v == 1})
    for m, v in query("qnapRaidStatus"):
        states.append({"host": "QNAP", "name": m["raidName"].removeprefix("/dev/"), "ok": v == 1})
    return states


def varint(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def mqtt_str(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


class Mqtt:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=30)
        body = mqtt_str("MQTT") + bytes([4, 2]) + struct.pack("!H", 60) + mqtt_str("disk-health")
        self.sock.sendall(b"\x10" + varint(len(body)) + body)
        ack = self.sock.recv(4)
        if len(ack) < 4 or ack[3] != 0:
            raise RuntimeError(f"MQTT connect refused: {ack!r}")

    def publish(self, topic, payload):
        body = mqtt_str(topic) + payload.encode()
        self.sock.sendall(b"\x31" + varint(len(body)) + body)

    def close(self):
        self.sock.sendall(b"\xe0\x00")
        self.sock.close()


def discovery(mqtt, d):
    uid = f"diskhealth_{d['key']}"
    state_topic = f"diskhealth/{d['key']}/state"
    device = {"identifiers": [uid], "name": f"{d['host']} {d['disk']}", "manufacturer": d["host"]}
    if d["model"]:
        device["model"] = d["model"]
    if d["serial"]:
        device["serial_number"] = d["serial"]
    entities = [
        ("status", {"name": "Status", "value_template": "{{ value_json.status }}", "json_attributes_topic": state_topic, "icon": "mdi:harddisk"}),
        ("temperature", {"name": "Temperature", "value_template": "{{ value_json.temperature }}", "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ]
    if d.get("life_used") is not None:
        entities.append(("life_used", {"name": "Wear", "value_template": "{{ value_json.life_used }}", "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:gauge"}))
    if d.get("reallocated") is not None or d.get("bad_sectors") is not None:
        entities.append(("bad_sectors", {"name": "Bad sectors", "value_template": "{{ value_json.reallocated if value_json.reallocated is not none else value_json.bad_sectors }}", "state_class": "measurement", "icon": "mdi:alert-octagon-outline"}))
    for obj, cfg in entities:
        cfg.update({"unique_id": f"{uid}_{obj}", "state_topic": state_topic, "device": device, "expire_after": 1800})
        mqtt.publish(f"homeassistant/sensor/{uid}/{obj}/config", json.dumps(cfg))
    mqtt.publish(state_topic, json.dumps(d))


def summary(mqtt, disks, notes, storage):
    bad_storage = [f"{s['host']} {s['name']}" for s in storage if not s["ok"]]
    replace = [f"{d['host']} {d['disk']} ({d['reasons']})" for d in disks if d["status"] == "Replace"]
    watch = [f"{d['host']} {d['disk']} ({d['reasons']})" for d in disks if d["status"] == "Watch"]
    state = {
        "replace": len(replace),
        "watch": len(watch) + len(notes) + len(bad_storage),
        "replace_list": replace,
        "watch_list": watch,
        "qnap_smart": notes,
        "storage_problems": bad_storage,
        "disks": len(disks),
    }
    device = {"identifiers": ["diskhealth_summary"], "name": "Disk health", "manufacturer": "disk-health"}
    for obj, name, tmpl, icon in (
        ("replace", "Disks to replace", "{{ value_json.replace }}", "mdi:harddisk-remove"),
        ("watch", "Disks to watch", "{{ value_json.watch }}", "mdi:harddisk-plus"),
    ):
        cfg = {
            "name": name, "unique_id": f"diskhealth_summary_{obj}",
            "state_topic": "diskhealth/summary/state", "value_template": tmpl, "json_attributes_topic": "diskhealth/summary/state",
            "state_class": "measurement", "icon": icon, "device": device, "expire_after": 1800,
        }
        mqtt.publish(f"homeassistant/sensor/diskhealth_summary/{obj}/config", json.dumps(cfg))
    mqtt.publish("diskhealth/summary/state", json.dumps(state))
    return state


def main():
    disks = node_disks() + synology_disks()
    qdisks, notes = qnap_disks()
    disks += qdisks
    storage = storage_states()
    if os.environ.get("DRY_RUN"):
        class Null:
            def publish(self, topic, payload):
                pass
        state = summary(Null(), disks, notes, storage)
    else:
        mqtt = Mqtt(MQTT_HOST, MQTT_PORT)
        try:
            for d in sorted(disks, key=lambda d: (d["host"], d["disk"])):
                discovery(mqtt, d)
            state = summary(mqtt, disks, notes, storage)
        finally:
            mqtt.close()
    for d in sorted(disks, key=lambda d: (-RANK[d["status"]], d["host"], d["disk"])):
        print(f"{d['status']:7} {d['host']:6} {d['disk']:10} {d['model'][:32]:32} {d['reasons']}")
    print(json.dumps({k: v for k, v in state.items() if k != "disks"}))


if __name__ == "__main__":
    main()
