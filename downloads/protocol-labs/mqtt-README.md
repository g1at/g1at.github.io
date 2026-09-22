# MQTT raw-wire lab

This lab uses an isolated MQTT 3.1.1 listener on `127.0.0.1:18891`.  The
Python script depends only on the standard library.  It tests incremental TCP
framing, retained-message flag scope, persistent-session restoration, a
duplicated QoS 2 PUBLISH, Packet Identifier reuse after PUBCOMP, Keep Alive
timeout/Will publication, and graceful DISCONNECT suppression of the Will.

Start the broker:

```bash
mosquitto -c mqtt-mosquitto.conf -v
```

In another terminal:

```bash
python3 mqtt_deep_lab.py
```

Use only an isolated lab broker.  The script uses topics below `lab/deep/` and
client IDs beginning with `mqtt-depth-`.  Mosquitto 2.0.18 was used for the
recorded output in the article.
