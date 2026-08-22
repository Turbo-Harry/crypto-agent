#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口：事件实现已下沉 execution，避免 engines 反向依赖 service。"""
from execution.events import EVENTS_FILE, event_path, log_event, tail_events

__all__ = ["EVENTS_FILE", "event_path", "log_event", "tail_events"]
