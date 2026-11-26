from adofai_publisher.resources import icon_path


def test_icon_assets_exist() -> None:
    assert icon_path("app-logo.png").is_file()
    assert icon_path("app-icon.ico").is_file()
