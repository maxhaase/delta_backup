#!/usr/bin/env python3
# ==============================================================
# Project: delta-backup - Safe cold backup of host and VMs
# Author: Max Haase – maxhaase@gmail.com
# License: MIT
# ==============================================================

import os, subprocess, shlex, time, socket, sys, shutil, configparser
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

