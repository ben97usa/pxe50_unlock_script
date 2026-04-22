#!/usr/bin/env python3
# This work on April 21, 2024 was done by Ben
# Collect ONLY GPCARDSN.CSR from each GP card to PXE folder
# Fixed version:
# - FRU command fixed: no wrong quote around "fru print 2"
# - better debug for FRU output
# - safer CSR check
# - safer SCP verify
# - retry / timeout kept

import os
import re
import subprocess
import sys
import time
from datetime import datetime

import pexpect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAC_FILE = os.path.join(BASE_DIR, "RM_MAC.txt")

SSH_PASSWORD = "$pl3nd1D"
FIND_IP = "/usr/local/bin/find_ip"

PXE_USER = "qsitoan"
PXE_IP = "192.168.202.50"
PXE_CSR_BASE = "/home/RMA_GPCARD/CSR"
PXE_PASSWORD = "QSI@qmf54321"

SLOT_TIMEOUT = 180
MAX_SLOT_RETRY = 2


def run(cmd):
    try:
        return subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            shell=True,
            universal_newlines=True
        )
    except subprocess.CalledProcessError as exc:
        return exc.output if exc.output else ""


def get_mac_from_file(filepath):
    with open(filepath, "r") as f:
        return f.read().strip()


def find_ip(mac):
    output = run("%s %s" % (FIND_IP, mac))
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
    if m:
        return m.group(1)
    return None


def exec_rm_cmd(ip, cmd):
    ssh_cmd = [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "root@%s" % ip,
        cmd
    ]

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )

        full_output = ""
        for line in proc.stdout:
            full_output += line

        proc.wait()
        return ("Completion Code: Success" in full_output, full_output)

    except Exception as e:
        return (False, "SSH execution failed: %s" % e)


def exec_cmd(ip, cmd):
    ssh_cmd = [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "root@%s" % ip,
        cmd
    ]

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )

        full_output = ""
        for line in proc.stdout:
            full_output += line

        proc.wait()
        return ("Completion Code: Success" in full_output, full_output)

    except Exception as e:
        return (False, "SSH execution failed: %s" % e)


def get_server_slots(manager_info):
    slots = []

    for line in manager_info.splitlines():
        line = line.strip()

        if not line.startswith("|"):
            continue
        if "Port State" in line:
            continue

        parts = [p.strip() for p in line.split("|") if p.strip()]

        if len(parts) < 7:
            continue

        slot = parts[0]
        port_type = parts[3]
        completion_code = parts[6]

        if port_type == "Server" and completion_code == "Success":
            try:
                slots.append(int(slot))
            except ValueError:
                pass

    return slots


def extract_board_serial(output):
    for line in output.splitlines():
        if "Board Serial" in line:
            return line.split(":", 1)[1].strip()
    return None


def get_today_folder(base_dir):
    folder_name = datetime.now().strftime("%B") + str(datetime.now().day)
    candidate = os.path.join(base_dir, folder_name)

    if not os.path.exists(candidate):
        os.makedirs(candidate)
        print("[OK] Created today's folder: %s" % candidate)
    else:
        print("[OK] Today's folder already exists: %s" % candidate)

    return candidate


def slot_timed_out(slot_start):
    return (time.time() - slot_start) > SLOT_TIMEOUT


def gp_login(ip, slot, slot_start):
    if slot_timed_out(slot_start):
        print("[TIMEOUT] slot %s stuck before GP login" % slot)
        return None

    print("[STEP] Opening SSH to RM %s for slot %s" % (ip, slot))

    child = pexpect.spawn(
        "sshpass -p '%s' ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@%s" % (SSH_PASSWORD, ip),
        encoding="utf-8",
        timeout=60
    )

    child.logfile = sys.stdout

    try:
        child.expect(r"#", timeout=30)
    except Exception:
        print("[FAIL] can't login to RM for slot %s" % slot)
        try:
            child.close()
        except Exception:
            pass
        return None

    if slot_timed_out(slot_start):
        print("[TIMEOUT] slot %s stuck after RM login" % slot)
        try:
            child.close()
        except Exception:
            pass
        return None

    print("[STEP] Trying to start serial session on slot %s port 8295" % slot)
    child.sendline("start serial session -i %s -p 8295" % slot)

    time.sleep(2)
    child.sendline("")

    try:
        child.expect(r"root@localhost:.*#", timeout=60)
    except Exception:
        print("[FAIL] can't login to 8295 on slot %s" % slot)
        try:
            child.close()
        except Exception:
            pass
        return None

    if slot_timed_out(slot_start):
        print("[TIMEOUT] slot %s stuck while entering 8295" % slot)
        try:
            child.close()
        except Exception:
            pass
        return None

    print("[OK] Entered GP console for slot %s" % slot)
    return child


def gp_exit(child):
    try:
        print("[STEP] Exiting GP / RM session")
        child.send("~.")
        child.expect(pexpect.EOF, timeout=10)
    except Exception:
        pass

    try:
        child.close()
    except Exception:
        pass


def gp_check_csr_file(child, gp_sn, slot, slot_start):
    if slot_timed_out(slot_start):
        print("[TIMEOUT] slot %s stuck before checking CSR file" % slot)
        return False

    csr_path = "/tmp/%s/%s.CSR" % (gp_sn, gp_sn)
    print("[STEP] Checking CSR file %s" % csr_path)

    child.sendline("test -f %s && echo CSR_OK || echo CSR_MISSING" % csr_path)

    try:
        child.expect(r"root@localhost:.*#", timeout=20)
    except Exception:
        print("[FAIL] can't see slot %s" % slot)
        return False

    if slot_timed_out(slot_start):
        print("[TIMEOUT] slot %s stuck while checking CSR file" % slot)
        return False

    output = child.before

    if "CSR_OK" in output:
        print("[OK] CSR file exists for slot %s (%s)" % (slot, gp_sn))
        return True

    print("[FAIL] CSR file not found for slot %s (%s)" % (slot, gp_sn))
    return False


def gp_scp_csr_to_pxe(child, gp_sn, dest_dir, pxe_password, slot, slot_start):
    if slot_timed_out(slot_start):
        print("[TIMEOUT] slot %s stuck before SCP" % slot)
        return False

    src = "/tmp/%s/%s.CSR" % (gp_sn, gp_sn)
    dest = "%s@%s:%s/" % (PXE_USER, PXE_IP, dest_dir)

    cmd = "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null %s %s" % (src, dest)

    print("[STEP] SCP CSR from GP to PXE")
    print("[CMD] %s" % cmd)

    child.sendline(cmd)

    while True:
        if slot_timed_out(slot_start):
            print("[TIMEOUT] slot %s stuck during SCP" % slot)
            return False

        idx = child.expect(
            [
                r"yes/no",
                r"[Pp]assword:",
                r"root@localhost:.*#",
                pexpect.TIMEOUT,
                pexpect.EOF
            ],
            timeout=30
        )

        if idx == 0:
            print("[INFO] SSH asking yes/no -> sending yes")
            child.sendline("yes")
            continue

        if idx == 1:
            print("[INFO] SCP asking PXE password -> sending password")
            child.sendline(pxe_password)
            continue

        if idx == 2:
            print("[OK] Back to GP prompt after SCP")
            break

        if idx == 3:
            print("[WARN] SCP still waiting for slot %s..." % slot)
            continue

        if idx == 4:
            print("[FAIL] SCP EOF for %s" % gp_sn)
            return False

    copied_file = os.path.join(dest_dir, "%s.CSR" % gp_sn)
    verify = run("test -f '%s' && echo OK || echo FAIL" % copied_file).strip()

    if "OK" in verify:
        print("[OK] Verified CSR exists on PXE: %s" % copied_file)
        return True

    print("[FAIL] CSR not found on PXE: %s" % copied_file)
    return False


def is_slot_not_present(output):
    lowered = output.lower()
    patterns = [
        "slot not present",
        "device not present",
        "target not present",
        "completion code: failure"
    ]
    for p in patterns:
        if p in lowered:
            return True
    return False


def process_one_slot(rm_ip, slot, dest_dir, pxe_password):
    slot_start = time.time()
    child = None

    try:
        print("\n--------------------------------------")
        print("[STEP] Getting GP serial from FRU for slot %s" % slot)
        print("--------------------------------------")

        fru_cmd = "set system cmd -i %s -c fru print 2" % slot
        print("[CMD] %s" % fru_cmd)

        ok, fru_output = exec_cmd(rm_ip, fru_cmd)
        print("[DEBUG] FRU raw output for slot %s:\n%s" % (slot, fru_output))

        if not ok:
            if is_slot_not_present(fru_output):
                return (False, "slot not present")
            return (False, "FRU command failed")

        gp_sn = extract_board_serial(fru_output)
        if not gp_sn:
            return (False, "No GP serial")

        print("[OK] GP SN = %s" % gp_sn)

        if slot_timed_out(slot_start):
            return (False, "slot timeout before GP login")

        child = gp_login(rm_ip, slot, slot_start)
        if child is None:
            return (False, "can't login to 8295")

        if slot_timed_out(slot_start):
            return (False, "slot timeout after GP login")

        if not gp_check_csr_file(child, gp_sn, slot, slot_start):
            if slot_timed_out(slot_start):
                return (False, "slot timeout checking CSR")
            return (False, "CSR file not found")

        if slot_timed_out(slot_start):
            return (False, "slot timeout before SCP")

        copied_ok = gp_scp_csr_to_pxe(child, gp_sn, dest_dir, pxe_password, slot, slot_start)
        if not copied_ok:
            if slot_timed_out(slot_start):
                return (False, "slot timeout during SCP")
            return (False, "SCP CSR failed")

        return (True, gp_sn)

    except Exception as e:
        return (False, "Exception: %s" % e)

    finally:
        if child is not None:
            gp_exit(child)


def main():
    pxe_password = PXE_PASSWORD

    print("[STEP] Reading RM MAC")
    print("[DEBUG] Using MAC file: %s" % MAC_FILE)

    rack_mac = get_mac_from_file(MAC_FILE)
    print("[INFO] RM MAC = %s" % rack_mac)

    print("[STEP] Finding RM IP")
    rm_ip = find_ip(rack_mac)
    if not rm_ip:
        print("[FAIL] Failed to find RM IP from RM_MAC.txt")
        sys.exit(1)

    print("[OK] RM IP = %s" % rm_ip)

    print("[STEP] Creating / using today's CSR folder on PXE")
    dest_dir = get_today_folder(PXE_CSR_BASE)
    final_name = os.path.basename(dest_dir)

    print("[OK] PXE destination folder: %s" % dest_dir)
    print("FINAL_CSR_FOLDER_NAME=%s" % final_name)

    print("[STEP] Running show manager info")
    ok, manager_info = exec_rm_cmd(rm_ip, "show manager info")

    if not ok:
        print("[FAIL] Failed to retrieve manager info from RM")
        print(manager_info)
        sys.exit(1)

    slots = get_server_slots(manager_info)
    print("[OK] Server slots to process: %s" % slots)

    if len(slots) == 0:
        print("[FAIL] No valid slots found")
        sys.exit(1)

    successes = []
    failures = []

    total = len(slots)
    current = 0

    for slot in slots:
        current += 1

        print("\n======================================")
        print("[STEP] Processing slot %s (%s/%s)" % (slot, current, total))
        print("======================================")

        slot_success = False
        last_reason = "Unknown failure"

        for attempt in range(1, MAX_SLOT_RETRY + 1):
            print("[STEP] Slot %s attempt %s/%s" % (slot, attempt, MAX_SLOT_RETRY))

            ok, result = process_one_slot(rm_ip, slot, dest_dir, pxe_password)

            if ok:
                gp_sn = result
                successes.append((slot, gp_sn))
                print("[OK] Copied %s.CSR to %s" % (gp_sn, dest_dir))
                slot_success = True
                break
            else:
                last_reason = result
                print("[WARN] Slot %s failed on attempt %s: %s" % (slot, attempt, result))

                if "slot not present" in result.lower():
                    print("[INFO] Slot %s does not exist / not present, skip retry" % slot)
                    break

                if "timeout" in result.lower():
                    print("[INFO] Slot %s hit global timeout" % slot)

                if attempt < MAX_SLOT_RETRY:
                    print("[INFO] Retrying slot %s ..." % slot)
                    time.sleep(2)

        if not slot_success:
            failures.append((slot, last_reason))

    print("\n======================================")
    print("Finished collecting CSR files to PXE")
    print("Destination folder: %s" % dest_dir)
    print("FINAL_CSR_FOLDER_NAME=%s" % final_name)
    print("======================================")

    print("Successful: %s" % len(successes))
    for slot, sn in successes:
        print("  slot %s: %s.CSR" % (slot, sn))

    print("Failed: %s" % len(failures))
    for slot, reason in failures:
        print("  slot %s: %s" % (slot, reason))


if __name__ == "__main__":
    main()
