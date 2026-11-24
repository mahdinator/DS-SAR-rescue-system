from sar.common.messages import Envelope, JobRequest, Sighting, from_body, to_body


def test_envelope_roundtrip():
    msg = JobRequest(drone="drone-1", x=1.5, y=2.0, done=[3, 4])
    env = Envelope(src="drone-1", seq=7, kind=msg.KIND, body=to_body(msg), dst="gs-1",
                   epoch=2, lamport=11, pos=(1.5, 2.0), ttl=3)
    back = Envelope.unpack(env.pack())
    assert back == env
    assert back.msg_id == "drone-1:7"
    assert back.message() == msg


def test_sighting_body_roundtrip():
    s = Sighting(sighting_id="drone-2#1", drone="drone-2", t=12.0, x=1.0, y=2.0, i=0, j=0,
                 confidence=0.7, signature=[0.1, 0.2], evidence=0.5, mode="search")
    assert from_body("sighting", to_body(s)) == s
