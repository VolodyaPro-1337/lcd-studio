from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget


class WindowOutput(QWidget):
    """Полноэкранное окно без рамки на выбранном мониторе.
    Подходит, если экран корпуса подключён как обычный дисплей (HDMI / USB-C)."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setCursor(Qt.BlankCursor)
        self.frame = None

    def show_on(self, screen):
        self.setGeometry(screen.geometry())
        self.show()

    def send(self, img: QImage):
        self.frame = img
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))
        if self.frame is not None:
            p.drawImage(QRectF(self.rect()), self.frame)


class Device:
    """Интерфейс драйвера экрана: open() -> bool, send(кадр), close().
    state — текст для пользователя, resolution — родное разрешение экрана или None."""

    name = ""
    state = ""
    resolution = None

    def open(self) -> bool:
        raise NotImplementedError

    def send(self, img: QImage):
        raise NotImplementedError

    def close(self):
        pass


from .usbscreen import UsbScreen  # noqa: E402

DEVICES: list[type] = [UsbScreen]
