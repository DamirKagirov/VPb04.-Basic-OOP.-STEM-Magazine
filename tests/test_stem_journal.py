"""Тесты объектной модели и JSON-хранилища.

Запуск из корня проекта::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stem_journal import (
    CatalogError,
    Customer,
    Issue,
    Journal,
    JsonStorage,
    Product,
    StateError,
    StorageError,
    Subscription,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELLED,
    SUBSCRIPTION_CLOSED,
    ValidationError,
)


def make_product(issue_count: int = 6, months: int = 12) -> Product:
    product = Product.create("8+", issue_count)
    for month in range(1, months + 1):
        Issue.create(product, 2026, month)
    return product


def make_customer(name: str = "Тестовый Клиент") -> Customer:
    return Customer.create(name, "test@example.com", "г. Тестовый, ул. Проверочная, д. 1")


class ProductTests(unittest.TestCase):
    def test_validation(self) -> None:
        with self.assertRaises(ValidationError):
            Product.create("6+", 6)
        with self.assertRaises(ValidationError):
            Product.create("8+", 7)

    def test_duplicate_month_is_rejected(self) -> None:
        product = Product.create("8+", 6)
        Issue.create(product, 2026, 1)
        with self.assertRaises(CatalogError):
            Issue.create(product, 2026, 1)
        self.assertEqual(len(product.issues), 1)

    def test_update_does_not_touch_existing_issues(self) -> None:
        product = make_product(6)
        before = list(product.issues)
        product.update(12)
        self.assertEqual(product.issue_count, 12)
        self.assertEqual(product.issues, before)

    def test_issue_at(self) -> None:
        product = make_product(6)
        self.assertIs(product.issue_at(2026, 3), product.issues[2])
        with self.assertRaises(CatalogError):
            product.issue_at(2027, 1)


class SubscriptionTests(unittest.TestCase):
    def test_create_assigns_first_package_and_adds_to_customer(self) -> None:
        product = make_product(6)
        customer = make_customer()
        subscription = Subscription.create(customer, product, 2026, 2)

        self.assertEqual(subscription.status, SUBSCRIPTION_ACTIVE)
        self.assertEqual(len(subscription.issues), 6)
        self.assertIs(subscription.issues[0], product.issues[1])
        self.assertEqual(customer.subscriptions, [subscription])

    def test_shared_issue_objects(self) -> None:
        product = make_product(6, months=8)
        first = Subscription.create(make_customer("Первый"), product, 2026, 1)
        second = Subscription.create(make_customer("Второй"), product, 2026, 3)

        self.assertIs(first.issues[2], second.issues[0])

    def test_buy_single_issue_creates_one_month_subscription(self) -> None:
        product = make_product(6)
        customer = make_customer()
        issue = product.issue_at(2026, 3)

        subscription = customer.buy_issue(product, issue)

        self.assertEqual(subscription.status, SUBSCRIPTION_ACTIVE)
        self.assertEqual(len(subscription.issues), 1)
        self.assertIs(subscription.issues[0], issue)
        self.assertEqual(customer.subscriptions, [subscription])
        self.assertEqual(subscription.period, "2026-03 — 2026-03")

    def test_buy_issue_through_journal(self) -> None:
        product = make_product(6)
        customer = make_customer()
        journal = Journal()
        journal.add_product(product)
        journal.add_customer(customer)

        subscription = journal.buy_issue(customer, product, product.issue_at(2026, 5))

        self.assertEqual(len(subscription.issues), 1)
        self.assertEqual(customer.subscriptions, [subscription])

    def test_buy_issue_from_foreign_product_is_rejected(self) -> None:
        product = make_product(6)
        other_product = make_product(6)
        customer = make_customer()

        with self.assertRaises(ValidationError):
            customer.buy_issue(product, other_product.issue_at(2026, 1))
        self.assertEqual(customer.subscriptions, [])

    def test_buy_issue_missing_from_catalog_is_rejected(self) -> None:
        product = make_product(6)
        customer = make_customer()
        detached = Issue(
            issue_id="ISS-999",
            product_id=product.product_id,
            issue_year=2027,
            issue_month=5,
        )

        with self.assertRaises(CatalogError):
            customer.buy_issue(product, detached)
        self.assertEqual(customer.subscriptions, [])

    def test_insufficient_catalog_does_not_create_subscription(self) -> None:
        product = make_product(6, months=5)
        customer = make_customer()

        with self.assertRaises(CatalogError):
            Subscription.create(customer, product, 2026, 1)
        self.assertEqual(customer.subscriptions, [])

    def test_renew_adds_next_package(self) -> None:
        product = make_product(6)
        subscription = Subscription.create(make_customer(), product, 2026, 1)

        added = subscription.renew()

        self.assertEqual(len(added), 6)
        self.assertEqual(added[0].issue_month, 7)
        self.assertEqual(len(subscription.issues), 12)

    def test_renew_without_enough_issues_leaves_subscription_unchanged(self) -> None:
        product = make_product(6, months=9)
        subscription = Subscription.create(make_customer(), product, 2026, 1)
        before = list(subscription.issues)

        with self.assertRaises(CatalogError):
            subscription.renew()
        self.assertEqual(subscription.issues, before)

    def test_cancel_preserves_issues(self) -> None:
        product = make_product(6)
        subscription = Subscription.create(make_customer(), product, 2026, 1)
        before = list(subscription.issues)

        subscription.cancel()

        self.assertEqual(subscription.status, SUBSCRIPTION_CANCELLED)
        self.assertEqual(subscription.issues, before)
        with self.assertRaises(StateError):
            subscription.renew()

    def test_close_requires_all_issues_released(self) -> None:
        product = make_product(6)
        subscription = Subscription.create(make_customer(), product, 2026, 1)

        for issue in subscription.issues[:-1]:
            issue.release()
        with self.assertRaises(StateError):
            subscription.close()

        subscription.issues[-1].release()
        subscription.close()
        self.assertEqual(subscription.status, SUBSCRIPTION_CLOSED)


class JsonStorageTests(unittest.TestCase):
    def test_round_trip_preserves_shared_issues_and_statuses(self) -> None:
        product = make_product(6)
        customer = make_customer()
        subscription = Subscription.create(customer, product, 2026, 1)
        subscription.issues[0].release()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            storage = JsonStorage(path)
            storage.save([product], [customer])

            loaded_products, loaded_customers = storage.load()

        loaded_product = loaded_products[0]
        loaded_subscription = loaded_customers[0].subscriptions[0]

        self.assertIs(loaded_subscription.issues[0], loaded_product.issues[0])
        self.assertEqual(loaded_subscription.issues[0].status, "released")
        self.assertEqual(loaded_subscription.status, SUBSCRIPTION_ACTIVE)
        self.assertEqual(loaded_product.edition, "8+")

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = JsonStorage(Path(directory) / "missing.json")
            with self.assertRaises(StorageError):
                storage.load()

    def test_single_issue_subscription_round_trip(self) -> None:
        product = make_product(6)
        customer = make_customer("Покупатель выпуска")
        subscription = customer.buy_issue(product, product.issue_at(2026, 5))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            storage = JsonStorage(path)
            storage.save([product], [customer])
            loaded_products, loaded_customers = storage.load()

        loaded_subscription = loaded_customers[0].subscriptions[0]
        self.assertEqual(loaded_subscription.subscription_id, subscription.subscription_id)
        self.assertEqual(len(loaded_subscription.issues), 1)
        self.assertIs(loaded_subscription.issues[0], loaded_products[0].issues[4])

    def test_bad_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(
                json.dumps({"version": 999, "products": [], "customers": []}),
                encoding="utf-8",
            )
            with self.assertRaises(StorageError):
                JsonStorage(path).load()

    def test_unknown_issue_reference_is_rejected(self) -> None:
        payload = {
            "version": 1,
            "products": [
                {
                    "product_id": "PRD-001",
                    "edition": "8+",
                    "issue_count": 6,
                    "issues": [],
                }
            ],
            "customers": [
                {
                    "customer_id": "CUS-001",
                    "name": "Клиент",
                    "email": "client@example.com",
                    "address": "Адрес",
                    "subscriptions": [
                        {
                            "subscription_id": "SUB-001",
                            "product_id": "PRD-001",
                            "issue_ids": ["ISS-404"],
                            "status": "active",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(StorageError):
                JsonStorage(path).load()


if __name__ == "__main__":
    unittest.main()
