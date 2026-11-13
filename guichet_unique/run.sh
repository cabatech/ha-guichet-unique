#!/bin/sh
# Traduit /data/options.json (options de l'add-on) en variables d'environnement.
set -e
eval "$(python3 - <<'EOF'
import json, shlex
o = json.load(open("/data/options.json"))
env = {
    "ESPACE_CITOYENS_BASE_URL": o["base_url"],
    "ESPACE_CITOYENS_USERNAME": o["username"],
    "ESPACE_CITOYENS_PASSWORD": o["password"],
    "ESPACE_CITOYENS_PERSON_ID": o["person_id"],
    "HA_CALENDAR_ENTITY": o["calendar_entity"],
    "GUICHET_CRON": o["cron"],
    "GUICHET_DEFAULT_LOCATION": o.get("school_address", ""),
    "GUICHET_PUBLIC_URL": o.get("public_url", ""),
    "MQTT_HOST": o.get("mqtt_host", ""),
    "MQTT_PORT": o["mqtt_port"],
    "MQTT_USERNAME": o.get("mqtt_username", ""),
    "MQTT_PASSWORD": o.get("mqtt_password", ""),
}
for k, v in env.items():
    print(f"export {k}={shlex.quote(str(v))}")
EOF
)"
exec guichet web --host 0.0.0.0 --port 8236
