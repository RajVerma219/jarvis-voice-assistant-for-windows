import sounddevice as sd, numpy as np, time, json
devs = sd.query_devices()
for i in [1,13,31]:
    try:
        d=sd.query_devices(i)
        print(f"ID {i}: {d['name']} | host={sd.query_hostapis(d['hostapi'])['name']} | in={d['max_input_channels']} defsr={d['default_samplerate']}")
    except Exception as e: print(i,e)
