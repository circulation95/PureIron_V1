from typing import Any

system: Any

# PureIron Example Scripts


# Tag Change Script: Auto-calculate average
# Triggered when [Memory]Line1/Sensor1 changes
def on_sensor1_change():
    v1 = system.tag.read("[Memory]Line1/Sensor1")
    v2 = system.tag.read("[Memory]Line1/Sensor2")
    if v1 is not None and v2 is not None:
        avg = (v1 + v2) / 2
        system.tag.write("[Memory]Line1/Average", avg)


# Timer Script: Heartbeat (every 10 seconds)
def heartbeat():
    import datetime

    system.tag.write("[Memory]System/Heartbeat", datetime.datetime.now().isoformat())
