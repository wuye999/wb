"""Windows scheduled tasks owned by this application."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

from sellerops.settings import paths as config

TASKS = [('SellerOps_Morning', '09:00', 'morning'), ('SellerOps_Check11', '11:00', 'check'),
         ('SellerOps_Check15', '15:00', 'check'), ('SellerOps_Check19', '19:00', 'check')]


def task_xml(at: str, mode: str) -> str:
    task = ET.Element('Task', version='1.2', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task')
    trigger = ET.SubElement(ET.SubElement(task, 'Triggers'), 'CalendarTrigger')
    ET.SubElement(trigger, 'StartBoundary').text = '2020-01-01T' + at + ':00'
    ET.SubElement(ET.SubElement(trigger, 'ScheduleByDay'), 'DaysInterval').text = '1'
    settings = ET.SubElement(task, 'Settings')
    ET.SubElement(settings, 'MultipleInstancesPolicy').text = 'IgnoreNew'
    ET.SubElement(settings, 'StartWhenAvailable').text = 'true'
    action = ET.SubElement(ET.SubElement(task, 'Actions'), 'Exec')
    ET.SubElement(action, 'Command').text = sys.executable
    ET.SubElement(action, 'Arguments').text = subprocess.list2cmdline(
        ['-m', 'sellerops', '--data-dir', config.DATA_DIR, 'daily', mode, '--apply'])
    ET.SubElement(action, 'WorkingDirectory').text = str(Path(__file__).resolve().parents[2])
    return ET.tostring(task, encoding='unicode')


def run(args) -> int:
    if not args.apply:
        for name, at, mode in TASKS:
            print('[dry-run]', '删除' if args.remove else '创建', name, at, mode)
        return 0
    if args.remove and not args.yes:
        print('[错误] 删除任务需要 --apply --yes')
        return 2
    if os.name != 'nt':
        print('[错误] 计划任务仅支持 Windows')
        return 2
    codes = []
    for name, at, mode in TASKS:
        if args.remove:
            result = subprocess.run(['schtasks', '/Delete', '/F', '/TN', name], capture_output=True)
        else:
            with tempfile.TemporaryDirectory(prefix='sellerops-task-') as tmp:
                xml_path = Path(tmp) / 'task.xml'
                xml_path.write_text(task_xml(at, mode), encoding='utf-16')
                result = subprocess.run(['schtasks', '/Create', '/F', '/TN', name, '/XML', str(xml_path)], capture_output=True)
        codes.append(result.returncode)
        print(name, 'OK' if result.returncode == 0 else 'FAIL')
    return 1 if any(codes) else 0
