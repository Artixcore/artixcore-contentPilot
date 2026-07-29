"""Smoke tests for the complete application entrypoint and route map."""

from __future__ import annotations

import importlib

from ui.navigation_complete import NAV_OPTIONS, PAGE_KEYS, PAGE_PERMISSIONS


def test_complete_entrypoint_imports_all_product_modules():
    module = importlib.import_module("app_complete")
    assert callable(module.main)
    assert callable(module.bootstrap_database)


def test_complete_navigation_routes_are_unique_and_permissioned():
    labels = [label for label, _key, _permission in NAV_OPTIONS]
    keys = [key for _label, key, _permission in NAV_OPTIONS]
    assert len(labels) == len(set(labels))
    assert len(keys) == len(set(keys))
    assert set(labels) == set(PAGE_PERMISSIONS)
    assert set(labels) == set(PAGE_KEYS)
    required = {
        "Workspaces",
        "Campaigns",
        "Analytics",
        "Leads",
        "Automations",
        "OAuth Integrations",
        "Brand Brain",
        "Security",
    }
    assert required.issubset(set(labels))
