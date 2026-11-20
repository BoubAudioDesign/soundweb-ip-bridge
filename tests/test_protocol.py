from soundweb_bridge import protocol as p


def test_sdk_example_set_value_button():
    # "SET_VALUE, Button, 1, 1 translates as: 2, 80, 0, 1, 0, 1, 80, 3" (BSS Serial Interface Kit)
    assert p.frame(p.build_set_value(0, 1, 1)) == bytes.fromhex("02 80 00 01 00 01 80 03")


def test_escaping_and_checksum():
    body = bytes([p.RAW_MSG, 0x02, 0x03, 0x06, 0x15, 0x1B])
    data = p.frame(body)
    assert data[0] == p.STX and data[-1] == p.ETX
    assert all(b not in p.SPECIAL for b in data[1:-1] if b != p.ESC)
    frames = list(p.Decoder().feed(data))
    assert frames == [p.Frame(body)]


def test_source_select_layout():
    body = p.build_source_select(0x05, 3)
    assert body == bytes.fromhex("84 00000005 01000800 0003")
    assert p.parse_body(body) == p.RawMessage(5, p.METHOD_SOURCE_SELECT, 3)


def test_preset_is_32bit_broadcast():
    assert p.build_preset(5) == bytes.fromhex("84 FFFFFFFF 0000000B 00000005")
    assert p.build_parameter_preset(0x12, 2) == bytes.fromhex("84 FFFFFFFF 0000000B 00120002")


def test_decoder_ack_nak_and_bad_checksum():
    good = p.frame(p.build_set_value(6, 2, 4))
    bad = bytearray(good)
    bad[-2] ^= 0x01
    out = list(p.Decoder().feed(bytes([p.ACK]) + good + bytes([p.NAK]) + bytes(bad)))
    assert out[0] == p.ACK
    assert out[1] == p.Frame(p.build_set_value(6, 2, 4))
    assert out[2] == p.NAK
    assert out[3] == p.Frame(None)


def test_decoder_split_across_chunks():
    data = p.frame(p.build_source_select(0x1B02, 2))
    dec = p.Decoder()
    out = []
    for b in data:
        out += list(dec.feed(bytes([b])))
    assert out == [p.Frame(p.build_source_select(0x1B02, 2))]
