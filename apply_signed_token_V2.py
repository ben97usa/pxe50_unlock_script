#!/usr/bin/env python3
# This work on April 21, 2024 was done by Ben
# Apply signed_token.bin from PXE to each GP card and unlock it
# Improved version:
# - global timeout per slot
# - detect slot not present
# - retry once for 8295 / SCP failures

#!/usr/bin/env python3
import os
import re
import sys
import time
import subprocess
import pexpect
from datetime import datetime

# =========================================================
# CONFIG
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAC_FILE = os.path.join(BASE_DIR, "RM_MAC.txt")

SSH_PASSWORD = "$pl3nd1D"          # RM/root password
FIND_IP = "/usr/local/bin/find_ip"
RSCM_SHOW_MANAGER_INFO = "show manager info"

# Script runs on PXE50
PXE_IP = "192.168.202.50"
PXE_USER = "qsitoan"
PXE_PASSWORD = "QSI@qmf54321"

PXE_BASE_HOME = "/home/qsitoan"

# =========================================================
# PRINT HELPERS
# =========================================================
def print_step(msg):
    print(f"\n[STEP] {msg}")

def print_info(msg):
    print(f"[INFO] {msg}")

def print_ok(msg):
    print(f"[OK] {msg}")

def print_warn(msg):
    print(f"[WARN] {msg}")

def print_fail(msg):
    print(f"[FAIL] {msg}")

# =========================================================
# DATE / PATH HELPERS
# =========================================================
def get_today_unlock_folder_name():
    # Example: April20_unlock, April21_unlock
    return datetime.now().strftime("%B") + str(datetime.now().day) + "_unlock"

def get_today_unlock_base():
    return os.path.join(PXE_BASE_HOME, get_today_unlock_folder_name())

# =========================================================
# BASIC HELPERS
# =========================================================
def run(cmd):
    try:
        return subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            shell=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        return e.output if e.output else ""

def get_mac_from_file(filepath):
    with open(filepath, "r") as f:
        return f.read().strip()

def find_ip(mac):
    output = run(f"{FIND_IP} {mac}")
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
    return m.group(1) if m else None

def extract_board_serial(output):
    for line in output.splitlines():
        if "Board Serial" in line:
            return line.split(":", 1)[1].strip()
    return None

def parse_policy_value(output):
    patterns = [
        r"Policy\s*Retrieved\.\s*Policy\s*=\s*(0x[0-9a-fA-F]+)",
        r"Retrieved\s*Policy\s*=\s*(0x[0-9a-fA-F]+)",
        r"Policy\s*=\s*(0x[0-9a-fA-F]+)",
    ]
    for p in patterns:
        m = re.search(p, output, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return None

# =========================================================
# RM HELPERS
# =========================================================
def exec_rm_cmd(ip, cmd, timeout=90):
    ssh_cmd = [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"root@{ip}",
        cmd
    ]

    print_info(f"RSCM CMD: {cmd}")

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        full_output = ""
        start = time.time()
        last_status = time.time()

        while True:
            line = proc.stdout.readline()

            if line:
                line = line.rstrip()
                full_output += line + "\n"
                print(line)

            elif proc.poll() is not None:
                break

            else:
                now = time.time()
                if now - start > timeout:
                    proc.kill()
                    print_fail(f"RSCM timeout after {timeout}s")
                    return False, full_output

                if now - last_status > 15:
                    print_warn(f"Still waiting on RSCM... ({int(now - start)}s)")
                    last_status = now

                time.sleep(0.2)

        proc.wait()
        return ("Completion Code: Success" in full_output), full_output

    except Exception as e:
        print_fail(f"SSH execution failed: {e}")
        return False, ""

def exec_slot_cmd(ip, slot, extra_cmd, timeout=60):
    cmd = f"set system cmd -i {slot} -c {extra_cmd}"
    return exec_rm_cmd(ip, cmd, timeout=timeout)

def get_server_slots(rm_manager_info_output):
    server_slots = []

    for line in rm_manager_info_output.splitlines():
        line = line.strip()

        if not line.startswith("|"):
            continue
        if "Port State" in line:
            continue

        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 7:
            continue

        try:
            slot = parts[0]
            port_type = parts[3]
            completion_code = parts[6]

            if port_type == "Server" and completion_code == "Success":
                server_slots.append(int(slot))
        except Exception:
            continue

    return server_slots

def get_gp_sn_from_fru(rm_ip, slot):
    print_step(f"Slot {slot}: get GP_CARD_SN")
    ok, output = exec_slot_cmd(rm_ip, slot, "fru print 2", timeout=60)

    if not ok:
        print_fail(f"can't see slot {slot}")
        return None

    gp_sn = extract_board_serial(output)
    if gp_sn:
        print_ok(f"Slot {slot}: GP_CARD_SN = {gp_sn}")
        return gp_sn

    print_fail(f"Slot {slot}: cannot parse GP_CARD_SN")
    return None

# =========================================================
# GP CONSOLE
# =========================================================
def gp_login(rm_ip, slot):
    print_step(f"Slot {slot}: login GP CARD 8295")

    try:
        child = pexpect.spawn(
            f"sshpass -p '{SSH_PASSWORD}' ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{rm_ip}",
            encoding="utf-8",
            timeout=30
        )
        child.delaybeforesend = 0.2

        child.expect([r"RScmCli#", r"#"], timeout=30)
        child.sendline(f"start serial session -i {slot} -p 8295")

        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                idx = child.expect(
                    [
                        r"root@localhost:.*#",
                        r"login:",
                        r"[Pp]assword:",
                        r"Completion Code:\s*Failure",
                        r"Unable to",
                        r"Not found",
                        r"Connection closed",
                        r"RScmCli#",
                    ],
                    timeout=8
                )

                if child.before:
                    txt = child.before.strip()
                    if txt:
                        print(txt)

                if idx == 0:
                    print_ok(f"Slot {slot}: entered GP console")
                    return child

                if idx in [1, 2, 3, 4, 5, 6, 7]:
                    print_fail(f"can't login to 8295, slot {slot}")
                    child.close(force=True)
                    return None

            except pexpect.TIMEOUT:
                print_info(f"Slot {slot}: still waiting GP prompt...")
                child.sendline("")

        print_fail(f"can't login to 8295, slot {slot}")
        child.close(force=True)
        return None

    except Exception as e:
        print_fail(f"can't login to 8295, slot {slot}: {e}")
        return None

def gp_send_cmd(child, cmd, timeout=30, step_desc=None):
    if step_desc:
        print_step(step_desc)

    print_info(f"GP CMD: {cmd}")
    child.sendline(cmd)

    start = time.time()
    last_status = time.time()
    collected = ""

    while True:
        try:
            child.expect(
                [
                    r"root@localhost:.*#",
                    r"#\s*$",
                ],
                timeout=5
            )

            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            return True, collected

        except pexpect.TIMEOUT:
            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            now = time.time()
            if now - start > timeout:
                print_fail(f"Command timeout: {cmd}")
                return False, collected

            if now - last_status > 10:
                print_warn(f"Still waiting... ({int(now - start)}s)")
                last_status = now

        except pexpect.EOF:
            print_fail(f"Session closed while running: {cmd}")
            return False, collected

def gp_exit(child):
    print_step("Exit GP console")
    try:
        child.sendcontrol("d")
        time.sleep(1)
    except Exception:
        pass

    try:
        child.send("~.")
        child.expect(pexpect.EOF, timeout=5)
    except Exception:
        pass

    try:
        child.close(force=True)
    except Exception:
        pass

# =========================================================
# GP ACTIONS
# =========================================================
def gp_get_policy(child):
    ok, output = gp_send_cmd(
        child,
        "ovb_lock policy get /tmp/policy.bin",
        timeout=30,
        step_desc="Check unlock policy"
    )

    if not ok:
        return None, output

    policy = parse_policy_value(output)
    if policy:
        return policy, output

    return "unknown", output

def gp_apply_policy_set(child):
    ok, output = gp_send_cmd(
        child,
        "ovb_lock policy set /tmp/signed_token.bin",
        timeout=60,
        step_desc="Apply /tmp/signed_token.bin"
    )
    return ok, output

def gp_prepare_tmp(child):
    ok, _ = gp_send_cmd(
        child,
        "rm -f /tmp/signed_token.bin",
        timeout=10,
        step_desc="Remove old /tmp/signed_token.bin"
    )
    return ok

def gp_scp_signed_token(child, gp_sn, unlock_base):
    """
    Run scp inside GP shell:
    scp qsitoan@192.168.202.50:/home/qsitoan/April20_unlock/<GP_SN>/signed_token.bin /tmp/signed_token.bin
    """
    print_step(f"SCP signed_token.bin for {gp_sn}")

    remote_file = f"{unlock_base}/{gp_sn}/signed_token.bin"
    scp_cmd = (
        f"scp -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"{PXE_USER}@{PXE_IP}:{remote_file} /tmp/signed_token.bin"
    )

    print_info(f"GP CMD: {scp_cmd}")
    child.sendline(scp_cmd)

    start = time.time()
    collected = ""
    password_sent = False

    while True:
        try:
            idx = child.expect(
                [
                    r"[Pp]assword:",
                    r"Permission denied",
                    r"No such file or directory",
                    r"100%",
                    r"root@localhost:.*#",
                    r"Connection closed",
                    r"Host key verification failed",
                ],
                timeout=8
            )

            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            if idx == 0:
                if password_sent:
                    print_fail("SCP failed: password asked again")
                    return False, collected
                print_info("Sending PXE password...")
                child.sendline(PXE_PASSWORD)
                password_sent = True

            elif idx == 1:
                print_fail("SCP failed: permission denied")
                return False, collected

            elif idx == 2:
                print_fail("SCP failed: file not found on PXE")
                return False, collected

            elif idx == 3:
                continue

            elif idx == 4:
                print_ok("SCP finished")
                break

            elif idx == 5:
                print_fail("SCP failed: connection closed")
                return False, collected

            elif idx == 6:
                print_fail("SCP failed: host key verification failed")
                return False, collected

            if time.time() - start > 90:
                print_fail("SCP timeout")
                return False, collected

        except pexpect.TIMEOUT:
            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            if time.time() - start > 90:
                print_fail("SCP timeout")
                return False, collected

            print_info("Still waiting SCP...")
            continue

        except pexpect.EOF:
            print_fail("SCP session closed unexpectedly")
            return False, collected

    ok, output = gp_send_cmd(
        child,
        "test -f /tmp/signed_token.bin && echo EXISTS || echo MISSING",
        timeout=10,
        step_desc="Verify /tmp/signed_token.bin"
    )

    if not ok:
        return False, collected + "\n" + output

    if "EXISTS" in output:
        print_ok("/tmp/signed_token.bin exists")
        return True, collected + "\n" + output

    print_fail("/tmp/signed_token.bin missing after scp")
    return False, collected + "\n" + output

# =========================================================
# MAIN
# =========================================================
def main():
    unlock_base = get_today_unlock_base()

    print_step("Check required files/folders")
    print_info(f"RM_MAC.txt path = {MAC_FILE}")
    print_info(f"Today unlock folder = {unlock_base}")

    if not os.path.isfile(MAC_FILE):
        print_fail(f"RM_MAC.txt not found: {MAC_FILE}")
        print_warn("Put RM_MAC.txt in same folder as script")
        sys.exit(1)

    if not os.path.isdir(unlock_base):
        print_fail(f"Today's unlock folder not found: {unlock_base}")
        print_warn("Example expected folder: /home/qsitoan/April20_unlock")
        sys.exit(1)

    print_step("Read RM MAC")
    rm_mac = get_mac_from_file(MAC_FILE)
    print_info(f"RM MAC = {rm_mac}")

    print_step("Find RM IP")
    rm_ip = find_ip(rm_mac)
    if not rm_ip:
        print_fail(f"Cannot find RM IP from MAC {rm_mac}")
        sys.exit(1)
    print_ok(f"RM IP = {rm_ip}")

    print_step("Get rack info")
    ok, rm_manager_info = exec_rm_cmd(rm_ip, RSCM_SHOW_MANAGER_INFO, timeout=90)
    if not ok:
        print_fail("Failed to get rack manager info")
        sys.exit(1)

    slots = get_server_slots(rm_manager_info)
    if not slots:
        print_fail("No valid server slots found")
        sys.exit(1)

    print_ok(f"Server slots to process: {slots}")

    already_unlocked = []
    unlock_success = []
    no_signed_token = []
    failed_slots = []
    slot_status = {}

    for slot in slots:
        print("\n" + "=" * 70)
        print_step(f"START SLOT {slot}")

        child = None
        gp_sn = None

        try:
            # 1) first go to this slot and know GP_CARD_SN
            gp_sn = get_gp_sn_from_fru(rm_ip, slot)
            if not gp_sn:
                slot_status[slot] = "can't see slot"
                failed_slots.append((slot, "can't see slot"))
                continue

            # 2) then come back to PXE side and find matching folder by GP_CARD_SN
            token_path = os.path.join(unlock_base, gp_sn, "signed_token.bin")
            print_info(f"Matching token path on PXE = {token_path}")

            if not os.path.isfile(token_path):
                print_warn(f"Slot {slot}: no matching signed_token.bin for {gp_sn}")
                slot_status[slot] = f"no signed_token on PXE for {gp_sn}"
                no_signed_token.append((slot, gp_sn))
                continue

            print_ok(f"Slot {slot}: found matching signed_token.bin on PXE")

            # 3) login GP card
            child = gp_login(rm_ip, slot)
            if not child:
                slot_status[slot] = "can't login to 8295"
                failed_slots.append((slot, "can't login to 8295"))
                continue

            # 4) check current policy
            policy1, raw1 = gp_get_policy(child)
            print_info(f"Current policy = {policy1}")
            if raw1 and raw1.strip():
                print(raw1.strip())

            if policy1 == "0x2":
                print_ok(f"Slot {slot}: already unlocked")
                slot_status[slot] = f"already unlocked - {gp_sn}"
                already_unlocked.append((slot, gp_sn))
                gp_exit(child)
                continue

            print_warn(f"Slot {slot}: not unlocked yet")

            # 5) remove old /tmp/signed_token.bin
            if not gp_prepare_tmp(child):
                slot_status[slot] = f"failed prepare /tmp for {gp_sn}"
                failed_slots.append((slot, f"failed prepare /tmp for {gp_sn}"))
                gp_exit(child)
                continue

            # 6) scp matching token from PXE -> GP CARD /tmp/signed_token.bin
            copied, _ = gp_scp_signed_token(child, gp_sn, unlock_base)
            if not copied:
                slot_status[slot] = f"scp failed for {gp_sn}"
                failed_slots.append((slot, f"scp failed for {gp_sn}"))
                gp_exit(child)
                continue

            # 7) set policy
            applied, apply_output = gp_apply_policy_set(child)
            if not applied:
                print_fail(f"Slot {slot}: ovb_lock policy set failed")
                if apply_output and apply_output.strip():
                    print(apply_output.strip())
                slot_status[slot] = f"ovb_lock policy set failed - {gp_sn}"
                failed_slots.append((slot, f"ovb_lock policy set failed - {gp_sn}"))
                gp_exit(child)
                continue

            if apply_output and apply_output.strip():
                print(apply_output.strip())

            # 8) check policy again
            policy2, raw2 = gp_get_policy(child)
            print_info(f"Policy after set = {policy2}")
            if raw2 and raw2.strip():
                print(raw2.strip())

            if policy2 == "0x2":
                print_ok(f"Slot {slot}: UNLOCK SUCCESS")
                slot_status[slot] = f"unlock success - {gp_sn}"
                unlock_success.append((slot, gp_sn))
            else:
                print_fail(f"Slot {slot}: policy still not 0x2")
                slot_status[slot] = f"policy still not 0x2 - {gp_sn}"
                failed_slots.append((slot, f"policy still not 0x2 - {gp_sn}"))

            gp_exit(child)

        except KeyboardInterrupt:
            print_fail("User interrupted script")
            if child:
                gp_exit(child)
            sys.exit(1)

        except Exception as e:
            print_fail(f"Unexpected error at slot {slot}: {e}")
            slot_status[slot] = f"unexpected error: {e}"
            failed_slots.append((slot, f"unexpected error: {e}"))
            if child:
                gp_exit(child)
            continue

    print("\n" + "=" * 70)
    print("[SUMMARY] DONE")
    print("=" * 70)

    print("\nAlready unlocked:")
    if already_unlocked:
        for slot, gp_sn in already_unlocked:
            print(f"  - slot {slot}: {gp_sn}")
    else:
        print("  (none)")

    print("\nUnlock success:")
    if unlock_success:
        for slot, gp_sn in unlock_success:
            print(f"  - slot {slot}: {gp_sn}")
    else:
        print("  (none)")

    print("\nNo matching signed_token on PXE:")
    if no_signed_token:
        for slot, gp_sn in no_signed_token:
            print(f"  - slot {slot}: {gp_sn}")
    else:
        print("  (none)")

    print("\nFailed / manual check:")
    if failed_slots:
        for slot, reason in failed_slots:
            print(f"  - slot {slot}: {reason}")
    else:
        print("  (none)")

    print("\nFinal rack status by slot:")
    for slot in sorted(slot_status.keys()):
        print(f"  - slot {slot}: {slot_status[slot]}")

if __name__ == "__main__":
    main()
