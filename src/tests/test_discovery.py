from bamboozle.discovery import parse_announcement

P1S_PACKET = (
    b"NOTIFY * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1990\r\n"
    b"Server: UPnP/1.0\r\n"
    b"Location: 192.168.0.103\r\n"
    b"NT: urn:bambulab-com:device:3dprinter:1\r\n"
    b"NTS: ssdp:alive\r\n"
    b"USN: 01P00C511100150\r\n"
    b"Cache-Control: max-age=1800\r\n"
    b"DevModel.bambu.com: C12\r\n"
    b"DevName.bambu.com: P1S\r\n"
    b"DevSignal.bambu.com: -45\r\n"
    b"DevConnect.bambu.com: lan\r\n"
    b"DevBind.bambu.com: free\r\n\r\n"
)


def test_parses_bambu_announcement():
    ann = parse_announcement(P1S_PACKET)
    assert ann is not None
    assert ann.ip == "192.168.0.103"
    assert ann.serial == "01P00C511100150"
    assert ann.model == "C12"
    assert ann.name == "P1S"


def test_header_names_are_case_insensitive():
    ann = parse_announcement(
        b"NOTIFY * HTTP/1.1\r\nlocation: 192.168.0.100\r\nusn: 31B8BP5A1001391\r\n"
        b"DEVMODEL.BAMBU.COM: O1C2\r\n\r\n"
    )
    assert ann is not None
    assert (ann.ip, ann.serial, ann.model, ann.name) == (
        "192.168.0.100", "31B8BP5A1001391", "O1C2", "")


def test_rejects_packet_without_serial_or_ip():
    assert parse_announcement(b"NOTIFY * HTTP/1.1\r\nLocation: 192.168.0.1\r\n\r\n") is None
    assert parse_announcement(b"NOTIFY * HTTP/1.1\r\nUSN: 01P00C511100150\r\n\r\n") is None


def test_rejects_non_ip_location_and_junk():
    assert parse_announcement(
        b"NOTIFY * HTTP/1.1\r\nLocation: evil.example.com\r\nUSN: 01P00C511100150\r\n\r\n"
    ) is None
    assert parse_announcement(b"\xff\xfe\x00garbage") is None
    assert parse_announcement(
        b"NOTIFY * HTTP/1.1\r\nLocation: 192.168.0.5\r\nUSN: ../../etc\r\n\r\n"
    ) is None
