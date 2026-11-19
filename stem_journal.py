#!/usr/bin/env python3
"""Система управления подписками на научно-популярный журнал STEM.

Реализация учебной объектной модели из ``architecture.md``:

* :class:`Product`      — продукт подписки (возрастная линейка и размер пакета);
* :class:`Issue`        — выпуск журнала;
* :class:`Customer`     — клиент;
* :class:`Subscription` — подписка клиента на продукт.

Дополнительно модуль содержит:

* :class:`JsonStorage`  — сохранение и загрузка данных в формате JSON;
* :class:`Journal`      — небольшой фасад для работы с каталогом и клиентами.

В конце файла находится демонстрация работы системы. Запуск::

    python stem_journal.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Константы предметной области
# ---------------------------------------------------------------------------

DATA_FILE = Path(__file__).resolve().parent / "data" / "subscriptions.json"

EDITIONS = ("8+", "12+")
ISSUE_COUNTS = (6, 12, 24)

ISSUE_PLANNED = "planned"
ISSUE_RELEASED = "released"
ISSUE_STATUSES = (ISSUE_PLANNED, ISSUE_RELEASED)

SUBSCRIPTION_ACTIVE = "active"
SUBSCRIPTION_CANCELLED = "cancelled"
SUBSCRIPTION_CLOSED = "closed"
SUBSCRIPTION_STATUSES = (
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELLED,
    SUBSCRIPTION_CLOSED,
)

STATUS_LABELS = {
    ISSUE_PLANNED: "запланирован",
    ISSUE_RELEASED: "вышел",
    SUBSCRIPTION_ACTIVE: "действует",
    SUBSCRIPTION_CANCELLED: "отменена",
    SUBSCRIPTION_CLOSED: "завершена",
}


# ---------------------------------------------------------------------------
# Ошибки предметной области
# ---------------------------------------------------------------------------


class StemJournalError(Exception):
    """Базовая ошибка системы подписок."""


class ValidationError(StemJournalError):
    """Переданы некорректные данные."""


class CatalogError(StemJournalError):
    """В каталоге продукта не хватает выпусков или нарушена его целостность."""


class StateError(StemJournalError):
    """Операция недопустима в текущем состоянии объекта."""


class StorageError(StemJournalError):
    """Ошибка чтения или записи файла данных."""


# ---------------------------------------------------------------------------
# Генерация идентификаторов
# ---------------------------------------------------------------------------


class _IdSequence:
    """Выдаёт последовательные идентификаторы вида ``PRD-001``."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        value = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = value
        return f"{prefix}-{value:03d}"

    def observe(self, prefix: str, identifier: str) -> None:
        """Учитывает идентификатор, прочитанный из JSON.

        Благодаря этому после загрузки данных новые объекты не получают
        идентификаторы, которые уже заняты.
        """
        match = re.fullmatch(rf"{re.escape(prefix)}-(\d+)", identifier)
        if match:
            number = int(match.group(1))
            self._counters[prefix] = max(self._counters.get(prefix, 0), number)


_ids = _IdSequence()


# ---------------------------------------------------------------------------
# Вспомогательные функции проверки
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Поле «{field_name}» должно быть непустой строкой")
    return value.strip()


def _integer(
    value: object,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"Поле «{field_name}» должно быть целым числом")
    if minimum is not None and value < minimum:
        raise ValidationError(f"Поле «{field_name}» не может быть меньше {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"Поле «{field_name}» не может быть больше {maximum}")
    return value


def _edition(value: object) -> str:
    edition = _text(value, "edition")
    if edition not in EDITIONS:
        raise ValidationError(
            f"Линейка должна быть одной из {', '.join(EDITIONS)}, а не «{edition}»"
        )
    return edition


def _issue_count(value: object) -> int:
    count = _integer(value, "issue_count")
    if count not in ISSUE_COUNTS:
        raise ValidationError(
            "Размер пакета должен быть одним из "
            f"{', '.join(map(str, ISSUE_COUNTS))}, а не {count}"
        )
    return count


def _year(value: object) -> int:
    return _integer(value, "год", minimum=1900, maximum=2200)


def _month(value: object) -> int:
    return _integer(value, "месяц", minimum=1, maximum=12)


def _email(value: object) -> str:
    email = _text(value, "email")
    if not _EMAIL_RE.fullmatch(email):
        raise ValidationError(f"Некорректный адрес электронной почты: «{email}»")
    return email


def _status(value: object, allowed: Sequence[str], field_name: str) -> str:
    status = _text(value, field_name)
    if status not in allowed:
        raise ValidationError(
            f"Поле «{field_name}» должно быть одним из "
            f"{', '.join(allowed)}, а не «{status}»"
        )
    return status


def _period(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


# ---------------------------------------------------------------------------
# Issue: выпуск журнала
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Issue:
    """Один номер журнала конкретного продукта.

    Объект выпуска существует в единственном экземпляре в каталоге продукта.
    Подписки хранят ссылки на этот объект, поэтому один и тот же выпуск может
    одновременно входить в подписки разных клиентов.
    """

    issue_id: str
    product_id: str
    issue_year: int
    issue_month: int
    status: str = ISSUE_PLANNED

    def __post_init__(self) -> None:
        self.issue_id = _text(self.issue_id, "issue_id")
        self.product_id = _text(self.product_id, "product_id")
        self.issue_year = _year(self.issue_year)
        self.issue_month = _month(self.issue_month)
        self.status = _status(self.status, ISSUE_STATUSES, "status")

    # -- Фабрика ---------------------------------------------------------

    @classmethod
    def create(cls, product: "Product", year: int, month: int) -> "Issue":
        """Создаёт запланированный выпуск и добавляет его в каталог продукта."""
        if not isinstance(product, Product):
            raise ValidationError("product должен быть объектом Product")

        issue = cls(
            issue_id=_ids.next("ISS"),
            product_id=product.product_id,
            issue_year=_year(year),
            issue_month=_month(month),
            status=ISSUE_PLANNED,
        )
        product.add_issue(issue)
        return issue

    # -- Операции --------------------------------------------------------

    def release(self) -> None:
        """Переводит выпуск из ``planned`` в ``released``."""
        if self.status == ISSUE_RELEASED:
            raise StateError(f"Выпуск {self.issue_id} уже вышел")
        self.status = ISSUE_RELEASED

    # -- Представление ---------------------------------------------------

    @property
    def period(self) -> str:
        """Период выпуска в формате ``ГГГГ-ММ``."""
        return _period(self.issue_year, self.issue_month)

    @property
    def is_released(self) -> bool:
        return self.status == ISSUE_RELEASED

    def __str__(self) -> str:
        return f"{self.issue_id} ({self.period})"


# ---------------------------------------------------------------------------
# Product: продукт подписки
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Product:
    """Возрастная линейка журнала с каталогом выпусков."""

    product_id: str
    edition: str
    issue_count: int
    issues: list[Issue] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.product_id = _text(self.product_id, "product_id")
        self.edition = _edition(self.edition)
        self.issue_count = _issue_count(self.issue_count)
        if not isinstance(self.issues, list) or any(
            not isinstance(issue, Issue) for issue in self.issues
        ):
            raise ValidationError("issues должен быть списком объектов Issue")

    # -- Фабрика ---------------------------------------------------------

    @classmethod
    def create(cls, edition: str, issue_count: int) -> "Product":
        """Создаёт продукт с пустым каталогом выпусков."""
        return cls(
            product_id=_ids.next("PRD"),
            edition=_edition(edition),
            issue_count=_issue_count(issue_count),
        )

    # -- Настройки продукта ----------------------------------------------

    def update(self, issue_count: int) -> None:
        """Меняет размер пакета для новых оформлений и продлений.

        Уже назначенные подпискам выпуски при этом не меняются.
        """
        self.issue_count = _issue_count(issue_count)

    def add_issue(self, issue: Issue) -> None:
        """Добавляет выпуск в каталог, если его там ещё нет."""
        if not isinstance(issue, Issue):
            raise ValidationError("issue должен быть объектом Issue")
        if issue.product_id != self.product_id:
            raise ValidationError(
                f"Выпуск {issue.issue_id} принадлежит продукту "
                f"{issue.product_id}, а не {self.product_id}"
            )
        if any(existing.issue_id == issue.issue_id for existing in self.issues):
            raise CatalogError(f"Выпуск {issue.issue_id} уже есть в каталоге продукта")
        if any(
            (existing.issue_year, existing.issue_month)
            == (issue.issue_year, issue.issue_month)
            for existing in self.issues
        ):
            raise CatalogError(
                f"В каталоге продукта {self.product_id} уже есть выпуск за "
                f"{issue.period}"
            )
        self.issues.append(issue)

    # -- Работа с каталогом ----------------------------------------------

    @property
    def sorted_issues(self) -> list[Issue]:
        """Выпуски, упорядоченные от ранних к поздним."""
        return sorted(self.issues, key=lambda item: (item.issue_year, item.issue_month))

    def find_issue(self, issue_id: str) -> Issue | None:
        return next((issue for issue in self.issues if issue.issue_id == issue_id), None)

    def issues_from(
        self, start_year: int, start_month: int, count: int | None = None
    ) -> list[Issue]:
        """Возвращает первые ``count`` выпусков начиная с указанного месяца.

        В учебной модели у продукта не больше одного выпуска в месяц. Если
        в каталоге не хватает выпусков на полный пакет, подбор не выполняется.
        """
        start_year = _year(start_year)
        start_month = _month(start_month)
        count = self.issue_count if count is None else _integer(
            count, "count", minimum=1
        )

        available = [
            issue
            for issue in self.sorted_issues
            if (issue.issue_year, issue.issue_month) >= (start_year, start_month)
        ]
        if len(available) < count:
            raise CatalogError(
                f"В каталоге продукта {self.product_id} ({self.edition}) недостаточно "
                f"выпусков: нужно {count}, доступно {len(available)} "
                f"начиная с {_period(start_year, start_month)}"
            )
        return available[:count]

    def issues_after(self, issue: Issue, count: int | None = None) -> list[Issue]:
        """Возвращает первые ``count`` выпусков строго после указанного."""
        if not isinstance(issue, Issue):
            raise ValidationError("issue должен быть объектом Issue")
        if issue.product_id != self.product_id:
            raise ValidationError(
                f"Выпуск {issue.issue_id} не принадлежит продукту {self.product_id}"
            )
        count = self.issue_count if count is None else _integer(
            count, "count", minimum=1
        )

        available = [
            candidate
            for candidate in self.sorted_issues
            if (candidate.issue_year, candidate.issue_month)
            > (issue.issue_year, issue.issue_month)
        ]
        if len(available) < count:
            raise CatalogError(
                f"В каталоге продукта {self.product_id} ({self.edition}) недостаточно "
                f"выпусков для продления: нужно {count}, доступно {len(available)} "
                f"после {issue.period}"
            )
        return available[:count]

    def __str__(self) -> str:
        return (
            f"{self.product_id}: линейка {self.edition}, "
            f"пакет {self.issue_count} выпусков, каталог {len(self.issues)}"
        )


# ---------------------------------------------------------------------------
# Subscription: подписка клиента
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Subscription:
    """Связь клиента с продуктом и набором конкретных выпусков."""

    subscription_id: str
    customer_id: str
    product_id: str
    issues: list[Issue]
    status: str
    product: Product = field(repr=False)

    def __post_init__(self) -> None:
        self.subscription_id = _text(self.subscription_id, "subscription_id")
        self.customer_id = _text(self.customer_id, "customer_id")
        self.product_id = _text(self.product_id, "product_id")
        self.status = _status(self.status, SUBSCRIPTION_STATUSES, "status")

        if not isinstance(self.product, Product):
            raise ValidationError("product должен быть объектом Product")
        if self.product.product_id != self.product_id:
            raise ValidationError(
                f"Продукт {self.product.product_id} не соответствует product_id "
                f"подписки {self.product_id}"
            )
        if not isinstance(self.issues, list) or any(
            not isinstance(issue, Issue) for issue in self.issues
        ):
            raise ValidationError("issues должен быть списком объектов Issue")
        if len({issue.issue_id for issue in self.issues}) != len(self.issues):
            raise ValidationError("В подписке не может быть повторяющихся выпусков")
        for issue in self.issues:
            if issue.product_id != self.product_id:
                raise ValidationError(
                    f"Выпуск {issue.issue_id} принадлежит другому продукту"
                )

    # -- Фабрика ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        customer: "Customer",
        product: Product,
        start_year: int,
        start_month: int,
    ) -> "Subscription":
        """Оформляет активную подписку и добавляет её клиенту.

        Из каталога продукта выбираются первые ``issue_count`` выпусков
        начиная с указанного месяца. Подписка хранит ссылки на эти объекты
        ``Issue``, а не их копии.
        """
        if not isinstance(customer, Customer):
            raise ValidationError("customer должен быть объектом Customer")
        if not isinstance(product, Product):
            raise ValidationError("product должен быть объектом Product")

        issues = product.issues_from(start_year, start_month)
        subscription = cls(
            subscription_id=_ids.next("SUB"),
            customer_id=customer.customer_id,
            product_id=product.product_id,
            issues=issues,
            status=SUBSCRIPTION_ACTIVE,
            product=product,
        )
        customer.add_subscription(subscription)
        return subscription

    # -- Операции --------------------------------------------------------

    def renew(self) -> list[Issue]:
        """Добавляет следующий пакет выпусков того же продукта.

        Новые выпуски берутся строго после последнего назначенного выпуска.
        Если в каталоге не хватает выпусков на полный пакет, подписка
        остаётся без изменений.
        """
        self._require_active("продлить")
        if not self.issues:
            raise StateError(
                f"Подписка {self.subscription_id} не содержит выпусков"
            )

        last_issue = max(
            self.issues, key=lambda issue: (issue.issue_year, issue.issue_month)
        )
        new_issues = self.product.issues_after(last_issue, self.product.issue_count)
        self.issues.extend(new_issues)
        return new_issues

    def cancel(self) -> None:
        """Досрочно отменяет активную подписку, сохраняя историю выпусков."""
        self._require_active("отменить")
        self.status = SUBSCRIPTION_CANCELLED

    def close(self) -> None:
        """Завершает активную подписку, если все её выпуски уже вышли."""
        self._require_active("завершить")
        pending = [issue for issue in self.issues if not issue.is_released]
        if pending:
            periods = ", ".join(issue.period for issue in pending)
            raise StateError(
                f"Нельзя завершить подписку {self.subscription_id}: "
                f"ещё не вышли выпуски {periods}"
            )
        self.status = SUBSCRIPTION_CLOSED

    def _require_active(self, action: str) -> None:
        if self.status != SUBSCRIPTION_ACTIVE:
            raise StateError(
                f"Нельзя {action} подписку {self.subscription_id}: "
                f"её статус — «{STATUS_LABELS[self.status]}»"
            )

    # -- Представление ---------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.status == SUBSCRIPTION_ACTIVE

    @property
    def first_issue(self) -> Issue | None:
        if not self.issues:
            return None
        return min(
            self.issues, key=lambda issue: (issue.issue_year, issue.issue_month)
        )

    @property
    def last_issue(self) -> Issue | None:
        if not self.issues:
            return None
        return max(
            self.issues, key=lambda issue: (issue.issue_year, issue.issue_month)
        )

    @property
    def period(self) -> str:
        if not self.issues:
            return "—"
        return f"{self.first_issue.period} — {self.last_issue.period}"

    @property
    def periods(self) -> str:
        return ", ".join(issue.period for issue in self.issues)

    def __str__(self) -> str:
        return (
            f"{self.subscription_id}: {self.product.edition}, "
            f"{len(self.issues)} вып. ({self.period}), "
            f"статус — {STATUS_LABELS[self.status]}"
        )


# ---------------------------------------------------------------------------
# Customer: клиент
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Customer:
    """Клиент журнала и история его подписок."""

    customer_id: str
    name: str
    email: str
    address: str
    subscriptions: list[Subscription] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.customer_id = _text(self.customer_id, "customer_id")
        self.name = _text(self.name, "name")
        self.email = _email(self.email)
        self.address = _text(self.address, "address")
        if not isinstance(self.subscriptions, list) or any(
            not isinstance(subscription, Subscription)
            for subscription in self.subscriptions
        ):
            raise ValidationError(
                "subscriptions должен быть списком объектов Subscription"
            )

    # -- Фабрика ---------------------------------------------------------

    @classmethod
    def create(cls, name: str, email: str, address: str) -> "Customer":
        """Создаёт клиента без подписок."""
        return cls(
            customer_id=_ids.next("CUS"),
            name=_text(name, "name"),
            email=_email(email),
            address=_text(address, "address"),
        )

    # -- Изменение контактных данных -------------------------------------

    def change_name(self, new_name: str) -> None:
        self.name = _text(new_name, "name")

    def change_email(self, new_email: str) -> None:
        self.email = _email(new_email)

    def change_address(self, new_address: str) -> None:
        self.address = _text(new_address, "address")

    # -- Подписки --------------------------------------------------------

    def add_subscription(self, subscription: Subscription) -> None:
        """Добавляет оформленную подписку в историю клиента."""
        if not isinstance(subscription, Subscription):
            raise ValidationError("subscription должен быть объектом Subscription")
        if subscription.customer_id != self.customer_id:
            raise ValidationError(
                f"Подписка {subscription.subscription_id} принадлежит клиенту "
                f"{subscription.customer_id}, а не {self.customer_id}"
            )
        if any(
            existing.subscription_id == subscription.subscription_id
            for existing in self.subscriptions
        ):
            raise ValidationError(
                f"Подписка {subscription.subscription_id} уже добавлена клиенту"
            )
        self.subscriptions.append(subscription)

    def active_subscriptions(self) -> list[Subscription]:
        return [
            subscription
            for subscription in self.subscriptions
            if subscription.is_active
        ]

    def __str__(self) -> str:
        return (
            f"{self.customer_id}: {self.name}, {self.email}, {self.address}, "
            f"подписок — {len(self.subscriptions)}"
        )


# ---------------------------------------------------------------------------
# JsonStorage: хранение данных в JSON
# ---------------------------------------------------------------------------


class JsonStorage:
    """Сохраняет и загружает продукты, выпуски, клиентов и подписки.

    Выпуски хранятся один раз — в каталоге своего продукта. Подписки
    ссылаются на них по ``issue_ids``, поэтому после загрузки объекты
    выпусков снова оказываются общими для всех подписок.
    """

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str] = DATA_FILE) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def save(
        self, products: Sequence[Product], customers: Sequence[Customer]
    ) -> Path:
        payload = {
            "version": self.VERSION,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "products": [self._product_to_dict(product) for product in products],
            "customers": [self._customer_to_dict(customer) for customer in customers],
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StorageError(
                f"Не удалось сохранить данные в {self.path}: {exc}"
            ) from exc
        return self.path

    def load(self) -> tuple[list[Product], list[Customer]]:
        if not self.path.is_file():
            raise StorageError(f"Файл данных не найден: {self.path}")

        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(
                f"Не удалось прочитать данные из {self.path}: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise StorageError("Корень JSON-файла должен быть объектом")
        if payload.get("version") != self.VERSION:
            raise StorageError(
                f"Неподдерживаемая версия формата: {payload.get('version')!r}"
            )

        try:
            return self._rebuild(payload)
        except StemJournalError as exc:
            raise StorageError(f"Некорректные данные в {self.path}: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"Некорректные данные в {self.path}: {exc}") from exc

    # -- Сериализация ----------------------------------------------------

    @staticmethod
    def _product_to_dict(product: Product) -> dict:
        return {
            "product_id": product.product_id,
            "edition": product.edition,
            "issue_count": product.issue_count,
            "issues": [
                {
                    "issue_id": issue.issue_id,
                    "issue_year": issue.issue_year,
                    "issue_month": issue.issue_month,
                    "status": issue.status,
                }
                for issue in product.sorted_issues
            ],
        }

    @staticmethod
    def _customer_to_dict(customer: Customer) -> dict:
        return {
            "customer_id": customer.customer_id,
            "name": customer.name,
            "email": customer.email,
            "address": customer.address,
            "subscriptions": [
                {
                    "subscription_id": subscription.subscription_id,
                    "product_id": subscription.product_id,
                    "issue_ids": [
                        issue.issue_id for issue in subscription.issues
                    ],
                    "status": subscription.status,
                }
                for subscription in customer.subscriptions
            ],
        }

    # -- Восстановление объектной модели ---------------------------------

    @staticmethod
    def _rebuild(payload: dict) -> tuple[list[Product], list[Customer]]:
        products: list[Product] = []
        products_by_id: dict[str, Product] = {}
        issues_by_id: dict[str, Issue] = {}

        for raw_product in payload.get("products", []):
            product = Product(
                product_id=raw_product["product_id"],
                edition=raw_product["edition"],
                issue_count=raw_product["issue_count"],
            )
            if product.product_id in products_by_id:
                raise StorageError(
                    f"Продукт {product.product_id} встречается в файле дважды"
                )

            for raw_issue in raw_product.get("issues", []):
                issue = Issue(
                    issue_id=raw_issue["issue_id"],
                    product_id=product.product_id,
                    issue_year=raw_issue["issue_year"],
                    issue_month=raw_issue["issue_month"],
                    status=raw_issue.get("status", ISSUE_PLANNED),
                )
                if issue.issue_id in issues_by_id:
                    raise StorageError(
                        f"Выпуск {issue.issue_id} встречается в файле дважды"
                    )
                product.add_issue(issue)
                issues_by_id[issue.issue_id] = issue
                _ids.observe("ISS", issue.issue_id)

            products.append(product)
            products_by_id[product.product_id] = product
            _ids.observe("PRD", product.product_id)

        customers: list[Customer] = []
        customers_by_id: dict[str, Customer] = {}

        for raw_customer in payload.get("customers", []):
            customer = Customer(
                customer_id=raw_customer["customer_id"],
                name=raw_customer["name"],
                email=raw_customer["email"],
                address=raw_customer["address"],
            )
            if customer.customer_id in customers_by_id:
                raise StorageError(
                    f"Клиент {customer.customer_id} встречается в файле дважды"
                )

            for raw_subscription in raw_customer.get("subscriptions", []):
                product = products_by_id.get(raw_subscription["product_id"])
                if product is None:
                    raise StorageError(
                        f"Подписка {raw_subscription.get('subscription_id')} "
                        f"ссылается на неизвестный продукт "
                        f"{raw_subscription['product_id']}"
                    )

                issue_ids = raw_subscription.get("issue_ids", [])
                if not isinstance(issue_ids, list):
                    raise StorageError(
                        f"Поле issue_ids подписки "
                        f"{raw_subscription.get('subscription_id')} должно быть списком"
                    )

                subscription_issues: list[Issue] = []
                seen: set[str] = set()
                for issue_id in issue_ids:
                    issue = issues_by_id.get(issue_id)
                    if issue is None:
                        raise StorageError(
                            f"Подписка {raw_subscription.get('subscription_id')} "
                            f"ссылается на неизвестный выпуск {issue_id}"
                        )
                    if issue_id in seen:
                        raise StorageError(
                            f"Выпуск {issue_id} повторяется в подписке "
                            f"{raw_subscription.get('subscription_id')}"
                        )
                    if issue.product_id != product.product_id:
                        raise StorageError(
                            f"Выпуск {issue_id} не принадлежит продукту "
                            f"{product.product_id}"
                        )
                    seen.add(issue_id)
                    subscription_issues.append(issue)

                subscription = Subscription(
                    subscription_id=raw_subscription["subscription_id"],
                    customer_id=customer.customer_id,
                    product_id=product.product_id,
                    issues=subscription_issues,
                    status=raw_subscription["status"],
                    product=product,
                )
                customer.add_subscription(subscription)
                _ids.observe("SUB", subscription.subscription_id)

            customers.append(customer)
            customers_by_id[customer.customer_id] = customer
            _ids.observe("CUS", customer.customer_id)

        return products, customers


# ---------------------------------------------------------------------------
# Journal: фасад для работы с системой
# ---------------------------------------------------------------------------


class Journal:
    """Хранит каталог продуктов и клиентов, связывает их с JSON-хранилищем."""

    def __init__(self, storage: JsonStorage | None = None) -> None:
        self.storage = storage
        self.products: list[Product] = []
        self.customers: list[Customer] = []

    # -- Добавление объектов ---------------------------------------------

    def add_product(self, product: Product) -> None:
        if not isinstance(product, Product):
            raise ValidationError("product должен быть объектом Product")
        if self.product_by_id(product.product_id) is not None:
            raise ValidationError(f"Продукт {product.product_id} уже добавлен")
        self.products.append(product)

    def add_customer(self, customer: Customer) -> None:
        if not isinstance(customer, Customer):
            raise ValidationError("customer должен быть объектом Customer")
        if self.customer_by_id(customer.customer_id) is not None:
            raise ValidationError(f"Клиент {customer.customer_id} уже добавлен")
        self.customers.append(customer)

    # -- Поиск ------------------------------------------------------------

    def product_by_id(self, product_id: str) -> Product | None:
        return next(
            (product for product in self.products if product.product_id == product_id),
            None,
        )

    def customer_by_id(self, customer_id: str) -> Customer | None:
        return next(
            (
                customer
                for customer in self.customers
                if customer.customer_id == customer_id
            ),
            None,
        )

    def require_product(self, product_id: str) -> Product:
        product = self.product_by_id(product_id)
        if product is None:
            raise ValidationError(f"Продукт {product_id} не найден")
        return product

    def require_customer(self, customer_id: str) -> Customer:
        customer = self.customer_by_id(customer_id)
        if customer is None:
            raise ValidationError(f"Клиент {customer_id} не найден")
        return customer

    # -- Оформление подписки ---------------------------------------------

    def subscribe(
        self,
        customer: Customer,
        product: Product,
        start_year: int,
        start_month: int,
    ) -> Subscription:
        if self.customer_by_id(customer.customer_id) is None:
            raise ValidationError(
                f"Клиент {customer.customer_id} не добавлен в журнал"
            )
        if self.product_by_id(product.product_id) is None:
            raise ValidationError(
                f"Продукт {product.product_id} не добавлен в журнал"
            )
        return Subscription.create(customer, product, start_year, start_month)

    # -- Хранение ---------------------------------------------------------

    def save(self) -> Path:
        if self.storage is None:
            raise StorageError("Хранилище не настроено")
        return self.storage.save(self.products, self.customers)

    def load(self) -> None:
        if self.storage is None:
            raise StorageError("Хранилище не настроено")
        products, customers = self.storage.load()
        self.products = products
        self.customers = customers


# ---------------------------------------------------------------------------
# Демонстрация работы системы
# ---------------------------------------------------------------------------


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _print_customer(customer: Customer) -> None:
    print(f"  {customer}")
    for subscription in customer.subscriptions:
        print(f"    - {subscription}")
        print(f"      выпуски: {subscription.periods}")


def run_demo() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass

    _print_header("ДЕМОНСТРАЦИЯ: подписки на журнал STEM")

    # Данные демонстрации всегда начинаются с чистого файла.
    storage = JsonStorage(DATA_FILE)
    if storage.exists():
        storage.path.unlink()
    journal = Journal(storage)

    # 1. Продукты -------------------------------------------------------
    _print_header("1. Создание продуктов")
    stem8 = Product.create("8+", 6)
    stem12 = Product.create("12+", 12)
    journal.add_product(stem8)
    journal.add_product(stem12)
    print(f"  {stem8}")
    print(f"  {stem12}")

    # 2. Каталог выпусков -----------------------------------------------
    _print_header("2. Наполнение каталога выпусками 2026–2027 годов")
    for month in range(1, 13):
        Issue.create(stem8, 2026, month)
        Issue.create(stem12, 2026, month)
    for month in (1, 2):
        Issue.create(stem8, 2027, month)
        Issue.create(stem12, 2027, month)

    print(f"  {stem8}")
    print(f"  {stem12}")
    print(
        "  Первые выпуски 8+: "
        + ", ".join(issue.period for issue in stem8.sorted_issues[:4])
    )

    # 3. Клиенты ---------------------------------------------------------
    _print_header("3. Создание клиентов")
    anna = Customer.create(
        "Анна Смирнова",
        "anna.smirnova@example.com",
        "г. Москва, ул. Лесная, д. 7",
    )
    maria = Customer.create(
        "Мария Ковалёва",
        "maria.kovaleva@example.com",
        "г. Санкт-Петербург, Невский пр., д. 12",
    )
    oleg = Customer.create(
        "Олег Петров",
        "oleg.petrov@example.com",
        "г. Казань, ул. Баумана, д. 3",
    )
    for customer in (anna, maria, oleg):
        journal.add_customer(customer)
        print(f"  {customer}")

    anna.change_address("г. Москва, Ленинградский пр., д. 25")
    anna.change_email("a.smirnova@example.com")
    print(f"  После изменения данных: {anna}")

    # 4. Подписка Анны ---------------------------------------------------
    _print_header("4. Анна оформляет подписку на продукт 8+ с января 2026 года")
    anna_subscription = journal.subscribe(anna, stem8, 2026, 1)
    print(f"  {anna_subscription}")
    print(f"  Назначенные выпуски: {anna_subscription.periods}")

    # 5. Подписка Марии: те же объекты выпусков -------------------------
    _print_header("5. Мария оформляет подписку на тот же продукт с марта 2026")
    maria_subscription = journal.subscribe(maria, stem8, 2026, 3)
    print(f"  {maria_subscription}")
    print(f"  Назначенные выпуски: {maria_subscription.periods}")
    shared_issue = anna_subscription.issues[2]
    print(
        "  Один и тот же объект выпуска в двух подписках: "
        f"{shared_issue.issue_id} — "
        f"{shared_issue is maria_subscription.issues[0]}"
    )

    # 6. Продление -------------------------------------------------------
    _print_header("6. Анна продлевает подписку: добавляется следующий пакет")
    added_issues = anna_subscription.renew()
    print(f"  Добавлены выпуски: {', '.join(issue.period for issue in added_issues)}")
    print(f"  {anna_subscription}")
    print(f"  Все выпуски: {anna_subscription.periods}")

    # 7. Изменение размера пакета и подписка Олега -----------------------
    _print_header(
        "7. Продукт 12+: изменение размера пакета и оформление подписки Олега"
    )
    print("  Попытка оформить подписку на 12 выпусков с сентября 2026 года:")
    try:
        journal.subscribe(oleg, stem12, 2026, 9)
    except CatalogError as exc:
        print(f"  Ожидаемая ошибка: {exc}")

    stem12.update(6)
    print(f"  После update: {stem12}")
    oleg_subscription = journal.subscribe(oleg, stem12, 2026, 9)
    print(f"  {oleg_subscription}")
    print(f"  Назначенные выпуски: {oleg_subscription.periods}")

    # 8. Жизненный цикл подписок ----------------------------------------
    _print_header("8. Статусы выпусков и подписок")
    print("  Анна: отмечаем вышедшими все выпуски, кроме последнего")
    for issue in anna_subscription.issues[:-1]:
        issue.release()
    print(f"  Выпуски Анны: {anna_subscription.periods}")
    print("  Попытка завершить подписку до выхода последнего номера:")
    try:
        anna_subscription.close()
    except StateError as exc:
        print(f"  Ожидаемая ошибка: {exc}")

    anna_subscription.issues[-1].release()
    anna_subscription.close()
    print(f"  {anna_subscription}")

    print("  Мария отменяет подписку досрочно:")
    maria_subscription.cancel()
    print(f"  {maria_subscription}")
    print(f"  Выпуски сохранены в истории: {maria_subscription.periods}")

    print("  Попытка продлить отменённую подписку:")
    try:
        maria_subscription.renew()
    except StateError as exc:
        print(f"  Ожидаемая ошибка: {exc}")

    print("  Попытка завершить отменённую подписку:")
    try:
        maria_subscription.close()
    except StateError as exc:
        print(f"  Ожидаемая ошибка: {exc}")

    # 9. Итоговое состояние ---------------------------------------------
    _print_header("9. Итоговое состояние клиентов")
    for customer in journal.customers:
        _print_customer(customer)

    # 10. Сохранение и загрузка JSON ------------------------------------
    _print_header("10. Сохранение в JSON и загрузка обратно")
    saved_path = journal.save()
    print(f"  Данные сохранены: {saved_path}")
    print(f"  Размер файла: {saved_path.stat().st_size} байт")

    restored = Journal(JsonStorage(saved_path))
    restored.load()
    print(
        f"  Загружено: продуктов — {len(restored.products)}, "
        f"клиентов — {len(restored.customers)}"
    )

    restored_anna = restored.require_customer(anna.customer_id)
    restored_product = restored.require_product(stem8.product_id)
    restored_subscription = restored_anna.subscriptions[0]
    print(f"  Подписка Анны после загрузки: {restored_subscription}")
    print(
        "  Ссылки на выпуски восстановлены как общие объекты: "
        f"{restored_subscription.issues[0] is restored_product.issues[0]}"
    )

    print()
    print("  Краткое содержимое JSON (первые 500 символов):")
    print("  " + "-" * 74)
    snippet = saved_path.read_text(encoding="utf-8")[:500]
    print("\n".join("  " + line for line in snippet.splitlines()))
    print("  ...")

    _print_header("ДЕМОНСТРАЦИЯ ЗАВЕРШЕНА")


if __name__ == "__main__":
    run_demo()
