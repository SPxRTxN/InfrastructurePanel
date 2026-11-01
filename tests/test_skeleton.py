import homelab_agent


def test_version():
    assert homelab_agent.__version__.count(".") == 2
