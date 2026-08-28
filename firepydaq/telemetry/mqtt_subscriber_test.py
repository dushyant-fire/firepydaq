from __future__ import annotations

import argparse
import json

import paho.mqtt.client as mqtt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic", default="firepydaq/#")
    args = parser.parse_args()

    def on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code.is_failure:
            raise RuntimeError(f"MQTT connect failed: {reason_code}")
        client.subscribe(args.topic, qos=1)

    def on_message(_client, _userdata, message):
        payload = json.loads(message.payload.decode("utf-8"))
        print(message.topic, json.dumps(payload, indent=2))

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="firepydaq-subscriber-test",
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, 30)
    client.loop_forever()


if __name__ == "__main__":
    main()
