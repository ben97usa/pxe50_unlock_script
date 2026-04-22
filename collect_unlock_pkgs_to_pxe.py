#!/usr/bin/env python3
# This work on April 21, 2024 was done by Ben
# Apply signed_token.bin from PXE to each GP card and unlock it
# Copy full /tmp/GP_CARD_SN folder from each GP card to PXE /home/qsitoan/April20_unlock
# Improved version:
# - stable MAC file path
# - global timeout per slot
# - detect slot not present
# - retry slot for 8295 / SCP failures
# - clearer debug logs
# - optional log file output

#!/usr/bin/env python3
import re
import os
import sys
import time
import subprocess
from datetime import datetime

import pexpect

# =========================================================
# CONFIG
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAC_FILE = os.path.join(BASE_DIR, "RM_MAC.txt")

SSH_PASSWORD = "$pl3nd1D"      # RSCM/root password
PXE_USER = "qsitoan"
PXE_IP = "192.168.202.50"
PXE_BASE_HOME = "/home/qsitoan"
PXE_PASSWORD = "QSI@qmf54321"

FIND_IP = "/usr/local/bin/find_ip"
RSCM_SHOW_MANAGER_INFO = "show manager info"

REQUIRED_FILES = [
    "cert_0.der",
    "cert_1.der",
    "cert_2.der",
    "cert_3.der",
    "token.bin",
]

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
# BASIC HELPERS
# =========================================================
def today_folder_name():
    return datetime.now().strftime("%B%d") + "_unlock"

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

def pxe_ssh(cmd):
    """
    SSH vao PXE bang user QSITE, nhung ep moi lenh phai chay trong /home/qsitoan
    """
    remote_cmd = f"cd {PXE_BASE_HOME} && {cmd}"
    full_cmd = (
        f"sshpass -p '{PXE_PASSWORD}' ssh "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"{PXE_USER}@{PXE_IP} \"{remote_cmd}\""
    )
    return run(full_cmd)

# =========================================================
# RSCM HELPERS
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
        last_output_time = time.time()

        while True:
            line = proc.stdout.readline()

            if line:
                line = line.rstrip()
                full_output += line + "\n"
                print(line)
                last_output_time = time.time()

            elif proc.poll() is not None:
                break

            else:
                now = time.time()

                if now - start > timeout:
                    proc.kill()
                    print_fail(f"RSCM command timeout/stuck after {timeout}s")
                    return False, full_output

                if now - last_output_time > 15:
                    print_warn(f"Still waiting on RSCM command... ({int(now - start)}s elapsed)")
                    last_output_time = now

                time.sleep(0.2)

        proc.wait()
        return ("Completion Code: Success" in full_output), full_output

    except Exception as e:
        print_fail(f"SSH execution failed: {e}")
        return False, ""

def exec_slot_cmd(ip, slot, extra_cmd, timeout=90):
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

def extract_board_serial(output):
    for line in output.splitlines():
        if "Board Serial" in line:
            return line.split(":", 1)[1].strip()
    return None

def get_gp_sn_from_fru(ip, slot):
    print_step(f"Slot {slot}: getting GP CARD SN from 'fru print 2'")
    success, output = exec_slot_cmd(ip, slot, "fru print 2", timeout=60)

    if not success:
        print_fail(f"Slot {slot}: failed to run 'fru print 2'")
        return None

    gp_sn = extract_board_serial(output)
    if gp_sn:
        print_ok(f"Slot {slot}: GP CARD SN = {gp_sn}")
    else:
        print_fail(f"Slot {slot}: cannot parse Board Serial")
    return gp_sn

# =========================================================
# GP CONSOLE HELPERS
# =========================================================
def gp_send_cmd(child, cmd, timeout=30, step_desc=None):
    if step_desc:
        print_step(step_desc)

    print_info(f"GP CMD: {cmd}")
    child.sendline(cmd)

    start = time.time()
    last_output_time = time.time()
    collected = ""

    while True:
        try:
            child.expect(
                [
                    r"root@localhost:/tmp/.*#",
                    r"root@localhost:/.*#",
                    r"root@localhost:.*#",
                    r"root@localhost#",
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
                print_fail(f"Command timeout/stuck after {timeout}s: {cmd}")
                if collected.strip():
                    print_warn("Last output before timeout:")
                    print(collected.strip())
                return False, collected

            if now - last_output_time > 10:
                print_warn(f"Still waiting for command to finish... ({int(now - start)}s elapsed)")
                last_output_time = now

        except pexpect.EOF:
            print_fail(f"Session closed unexpectedly while running: {cmd}")
            return False, collected

def gp_login(rm_ip, slot):
    print_step(f"Slot {slot}: login to GP CARD via 8295")

    try:
        child = pexpect.spawn(
            f"sshpass -p '{SSH_PASSWORD}' ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{rm_ip}",
            encoding="utf-8",
            timeout=30
        )
        child.delaybeforesend = 0.3

        child.expect(r"#", timeout=30)

        print_info(f"Starting serial session to slot {slot} port 8295")
        child.sendline(f"start serial session -i {slot} -p 8295")

        deadline = time.time() + 90
        collected = ""

        while time.time() < deadline:
            child.sendline("")

            try:
                idx = child.expect(
                    [
                        r"root@localhost:/tmp/.*#",
                        r"root@localhost:/.*#",
                        r"root@localhost:.*#",
                        r"root@localhost#",
                        r"login:",
                        r"[Pp]assword:",
                        r"Completion Code:\s*Failure",
                        r"Unable to",
                        r"Not found",
                        r"Connection closed",
                    ],
                    timeout=8
                )

                if child.before:
                    txt = child.before.strip()
                    if txt:
                        collected += txt + "\n"
                        print(txt)

                if idx in [0, 1, 2, 3]:
                    print_ok(f"Slot {slot}: entered GP console")
                    return child

                elif idx == 4:
                    print_fail(f"Slot {slot}: 8295 showed login prompt instead of root shell")
                    try:
                        child.close(force=True)
                    except Exception:
                        pass
                    return None

                elif idx == 5:
                    print_fail(f"Slot {slot}: 8295 asked for password unexpectedly")
                    try:
                        child.close(force=True)
                    except Exception:
                        pass
                    return None

                elif idx == 6:
                    print_fail(f"Slot {slot}: 8295 returned Completion Code Failure")
                    try:
                        child.close(force=True)
                    except Exception:
                        pass
                    return None

                else:
                    print_fail(f"Slot {slot}: 8295 returned unexpected error")
                    try:
                        child.close(force=True)
                    except Exception:
                        pass
                    return None

            except pexpect.TIMEOUT:
                if child.before:
                    txt = child.before.strip()
                    if txt:
                        collected += txt + "\n"
                        print_info("Still waiting for 8295 prompt...")
                        print(txt)

        print_fail(f"can't login to 8295 at slot {slot} (timeout/stuck)")
        if collected.strip():
            print_warn("Last output seen before timeout:")
            print(collected.strip())

        try:
            child.close(force=True)
        except Exception:
            pass
        return None

    except pexpect.TIMEOUT:
        print_fail(f"can't login to 8295 at slot {slot} (timeout/stuck)")
        return None
    except Exception as e:
        print_fail(f"can't login to 8295 at slot {slot}: {e}")
        return None

def gp_exit(child):
    print_step("Exiting GP console")
    try:
        child.send("~.")
        child.expect(pexpect.EOF, timeout=10)
    except Exception:
        pass

    try:
        child.close(force=True)
    except Exception:
        pass

def gp_get_policy(child):
    ok, output = gp_send_cmd(
        child,
        "ovb_lock policy get /tmp/policy.bin",
        timeout=30,
        step_desc="Checking unlock policy"
    )
    if not ok:
        return None

    if "Policy Retrieved. Policy=0x2" in output:
        return "0x2"

    m = re.search(r"Policy Retrieved\.\s*Policy=(0x[0-9a-fA-F]+)", output)
    if m:
        return m.group(1)

    return "UNKNOWN"

def gp_ensure_folder(child, gp_sn):
    ok, _ = gp_send_cmd(
        child,
        f"mkdir -p /tmp/{gp_sn}",
        timeout=20,
        step_desc=f"Ensuring folder /tmp/{gp_sn} exists"
    )
    return ok

def gp_check_folder_exists(child, gp_sn):
    ok, output = gp_send_cmd(
        child,
        f"test -d /tmp/{gp_sn} && echo EXISTS || echo MISSING",
        timeout=15,
        step_desc=f"Checking folder /tmp/{gp_sn}"
    )
    if not ok:
        return False
    return "EXISTS" in output

def gp_list_folder(child, gp_sn):
    return gp_send_cmd(
        child,
        f"cd /tmp/{gp_sn} && ls",
        timeout=20,
        step_desc=f"Listing files in /tmp/{gp_sn}"
    )

def gp_check_required_files(child, gp_sn):
    ok, output = gp_list_folder(child, gp_sn)
    if not ok:
        return [], REQUIRED_FILES[:]

    present = []
    missing = []

    for fname in REQUIRED_FILES:
        if re.search(rf"(^|\s){re.escape(fname)}($|\s)", output):
            present.append(fname)
        else:
            missing.append(fname)

    if present:
        print_ok(f"Found files: {', '.join(present)}")
    if missing:
        print_warn(f"Missing files: {', '.join(missing)}")

    return present, missing

def gp_disable_security(child):
    ok1, _ = gp_send_cmd(
        child,
        "setenforce 0",
        timeout=20,
        step_desc="Disabling SELinux enforcement"
    )
    ok2, _ = gp_send_cmd(
        child,
        "ov-firewall --disable",
        timeout=20,
        step_desc="Disabling firewall"
    )
    return ok1 and ok2

def gp_generate_missing_files(child, gp_sn):
    folder = f"/tmp/{gp_sn}"

    print_step(f"Generating missing files for {gp_sn}")

    ok, _ = gp_send_cmd(
        child,
        f"mkdir -p {folder}",
        timeout=15,
        step_desc=f"Creating folder {folder} if needed"
    )
    if not ok:
        return False

    ok, _ = gp_send_cmd(
        child,
        f"cd {folder}",
        timeout=15,
        step_desc=f"Changing directory to {folder}"
    )
    if not ok:
        return False

    if not gp_disable_security(child):
        return False

    ok, output = gp_send_cmd(
        child,
        "ovb_lock token /tmp/token.bin",
        timeout=60,
        step_desc="Generating token.bin"
    )
    if not ok:
        return False
    if "error" in output.lower():
        print_warn("ovb_lock token output contains 'error'")

    ok, output = gp_send_cmd(
        child,
        f"cd {folder} && cerberus_utility getcertchain 0",
        timeout=60,
        step_desc="Generating cert chain"
    )
    if not ok:
        return False
    if "error" in output.lower():
        print_warn("cerberus_utility getcertchain output contains 'error'")

    gp_send_cmd(
        child,
        f"mv -f /tmp/token.bin {folder}/token.bin 2>/dev/null || true",
        timeout=20,
        step_desc="Moving token.bin into target folder"
    )

    gp_send_cmd(
        child,
        f"mv -f /tmp/cert_*.der {folder}/ 2>/dev/null || true",
        timeout=20,
        step_desc="Moving cert files into target folder if needed"
    )

    return True

def gp_run_interactive_password_cmd(child, cmd, password, timeout=180, desc="interactive command"):
    print_step(desc)
    print_info(f"GP INTERACTIVE CMD: {cmd}")
    child.sendline(cmd)

    start = time.time()
    collected = ""
    last_status_time = time.time()

    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            print_fail(f"{desc}: timeout/stuck after {timeout}s")
            if collected.strip():
                print_warn("Last output before timeout:")
                print(collected.strip())
            return False, collected

        try:
            idx = child.expect(
                [
                    r"Are you sure you want to continue connecting \(yes/no(/\[fingerprint\])?\)\?",
                    r"[Pp]assword:",
                    r"root@localhost:/tmp/.*#",
                    r"root@localhost:/.*#",
                    r"root@localhost:.*#",
                    r"root@localhost#",
                    r"#\s*$",
                    pexpect.EOF,
                ],
                timeout=8
            )

            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            if idx == 0:
                print_info("SSH host key prompt detected, sending yes")
                child.sendline("yes")

            elif idx == 1:
                print_info("Password prompt detected, sending PXE password")
                child.sendline(password)

            elif idx in [2, 3, 4, 5, 6]:
                return True, collected

            elif idx == 7:
                print_fail(f"{desc}: session closed unexpectedly")
                return False, collected

        except pexpect.TIMEOUT:
            now = time.time()
            if now - last_status_time > 10:
                print_warn(f"{desc}: still waiting... ({int(elapsed)}s elapsed)")
                last_status_time = now

def gp_prepare_remote_pxe_folder(remote_dir):
    day_folder = os.path.basename(remote_dir)

    # vì pxe_ssh() đã tự cd /home/qsitoan rồi
    output = pxe_ssh(f"mkdir -p '{day_folder}'")

    # verify cũng chạy trong /home/qsitoan
    verify = pxe_ssh(f"test -d '{remote_dir}' && echo OK || echo FAIL")

    if "OK" in verify:
        print_ok(f"PXE folder ready: {remote_dir}")
        return True

    print_fail(f"Failed creating PXE folder: {remote_dir}")
    if output:
        print(output)
    if verify:
        print(verify)
    return False

def gp_scp_folder_to_pxe(child, gp_sn, remote_dir):
    src_folder = f"/tmp/{gp_sn}"
    cmd = (
        f"scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-r {src_folder} {PXE_USER}@{PXE_IP}:{remote_dir}"
    )

    ok, output = gp_run_interactive_password_cmd(
        child,
        cmd,
        PXE_PASSWORD,
        timeout=180,
        desc=f"SCP {src_folder} to PXE"
    )
    if not ok:
        return False

    verify = pxe_ssh(f"test -d '{remote_dir}/{gp_sn}' && echo OK || echo FAIL")
    if "OK" in verify:
        print_ok(f"SCP completed for {gp_sn}")
        return True

    print_fail(f"SCP failed for {gp_sn}")
    if output:
        print(output)
    if verify:
        print(verify)
    return False

# =========================================================
# MAIN
# =========================================================
def main():
    if not os.path.isfile(MAC_FILE):
        print_fail(f"Cannot find MAC file: {MAC_FILE}")
        sys.exit(1)

    print_step("Reading RM MAC")
    rm_mac = get_mac_from_file(MAC_FILE)
    print_info(f"RM MAC = {rm_mac}")

    print_step("Finding RM IP")
    rm_ip = find_ip(rm_mac)
    if not rm_ip:
        print_fail(f"Cannot find RM IP from MAC {rm_mac}")
        sys.exit(1)
    print_ok(f"RM IP = {rm_ip}")

    print_step("Getting rack manager info")
    ok, rm_manager_info = exec_rm_cmd(rm_ip, RSCM_SHOW_MANAGER_INFO, timeout=90)
    if not ok:
        print_fail(f"Failed to get manager info from RM {rm_mac} ({rm_ip})")
        sys.exit(1)

    slots = get_server_slots(rm_manager_info)
    if not slots:
        print_fail("No valid server slots found")
        sys.exit(1)

    print_ok(f"Server slots to process: {slots}")

    day_folder = today_folder_name()
    remote_dir = f"{PXE_BASE_HOME}/{day_folder}"
    print_info(f"PXE destination folder: {remote_dir}")

    already_unlocked = []
    copied_successfully = []
    failed_slots = []

    # prepare day folder once up front
    if not gp_prepare_remote_pxe_folder(remote_dir):
        print_fail("Cannot prepare PXE destination folder")
        sys.exit(1)

    for slot in slots:
        print("\n" + "=" * 70)
        print_step(f"START SLOT {slot}")

        gp_sn = None
        child = None

        try:
            gp_sn = get_gp_sn_from_fru(rm_ip, slot)
            if not gp_sn:
                print_fail(f"can't see slot {slot}")
                failed_slots.append((slot, "can't see slot / cannot get GP CARD SN"))
                continue

            child = gp_login(rm_ip, slot)
            if not child:
                failed_slots.append((slot, "can't login to 8295"))
                continue

            policy = gp_get_policy(child)
            if policy is None:
                failed_slots.append((slot, "cannot read policy"))
                gp_exit(child)
                continue

            print_info(f"Slot {slot}: policy = {policy}")

            if policy == "0x2":
                print_ok(f"slot {slot} already unlocked")
                already_unlocked.append((slot, gp_sn))
                gp_exit(child)
                continue

            folder_exists = gp_check_folder_exists(child, gp_sn)
            if not folder_exists:
                print_warn(f"/tmp/{gp_sn} does not exist, will create it")
                if not gp_ensure_folder(child, gp_sn):
                    failed_slots.append((slot, f"cannot create /tmp/{gp_sn}"))
                    gp_exit(child)
                    continue
            else:
                print_ok(f"Found folder /tmp/{gp_sn}")

            present, missing = gp_check_required_files(child, gp_sn)

            if missing:
                print_warn(f"Slot {slot}: missing files, will generate")
                generated = gp_generate_missing_files(child, gp_sn)
                if not generated:
                    failed_slots.append((slot, f"failed generating files for {gp_sn}"))
                    gp_exit(child)
                    continue

                present, missing = gp_check_required_files(child, gp_sn)

            if missing:
                print_fail(f"Slot {slot}: still missing files after generate: {', '.join(missing)}")
                failed_slots.append((slot, f"missing files after generate: {', '.join(missing)}"))
                gp_exit(child)
                continue

            copied = gp_scp_folder_to_pxe(child, gp_sn, remote_dir)
            if not copied:
                failed_slots.append((slot, f"scp failed for {gp_sn}"))
                gp_exit(child)
                continue

            copied_successfully.append((slot, gp_sn))
            print_ok(f"Slot {slot}: copied successfully to PXE")
            gp_exit(child)

        except KeyboardInterrupt:
            print_fail("User interrupted script")
            if child:
                gp_exit(child)
            sys.exit(1)

        except Exception as e:
            print_fail(f"Unexpected error at slot {slot}: {e}")
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

    print("\nCopied successfully:")
    if copied_successfully:
        for slot, gp_sn in copied_successfully:
            print(f"  - slot {slot}: {gp_sn}")
    else:
        print("  (none)")

    print("\nFailed / manual check:")
    if failed_slots:
        for slot, reason in failed_slots:
            print(f"  - slot {slot}: {reason}")
    else:
        print("  (none)")

    print(f"\nPXE target folder: {remote_dir}")

if __name__ == "__main__":
    main()
