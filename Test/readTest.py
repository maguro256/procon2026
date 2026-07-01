import nfc

def on_connect(tag):
    print(f"Tag Type: {tag.type}")
    print(f"ID: {tag.identifier.hex()}")
    if tag.type == "3":  # Type3 = FeliCa
        print(f"IDm: {tag.identifier.hex()}")
    return True
while(1):
    with nfc.ContactlessFrontend('usb') as clf:
        clf.connect(rdwr={'on-connect': on_connect})