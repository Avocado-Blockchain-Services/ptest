from src.shared import VALUE


def test_alpha_tolerates_benign_shared_change():
    assert VALUE in {1, 2}
