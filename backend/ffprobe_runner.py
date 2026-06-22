import subprocess
import json
import time
import threading
from pathlib import Path
from typing import List, Optional, Callable
from pydantic import BaseModel


class ProbeResult(BaseModel):
    success: bool
    data: Optional[dict]
    tried_path: Optional[str]
    tried_extension: Optional[str]
    error: Optional[str]
    error_type: Optional[str]
    raw_stderr: Optional[str]


def _kill_proc(proc: subprocess.Popen):
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _run_ffprobe_one(
    target_path: Path,
    timeout: int,
    stop_event: Optional[threading.Event],
    log: Callable[[str], None]
) -> ProbeResult:
    if stop_event and stop_event.is_set():
        return ProbeResult(success=False, data=None, tried_path=str(target_path), tried_extension=target_path.suffix, error="cancelled", error_type="cancelled", raw_stderr=None)
        
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        "-analyzeduration", "2000000",
        "-probesize", "2000000",
        str(target_path),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        
        deadline = time.monotonic() + timeout
        
        while True:
            try:
                # 使用带 timeout 的 communicate 可以持续读取管道数据，防止由于 stdout 数据量超过 64KB 导致管道阻塞死锁！
                stdout, stderr = proc.communicate(timeout=0.2)
                ret = proc.returncode
                break
            except subprocess.TimeoutExpired:
                if stop_event and stop_event.is_set():
                    _kill_proc(proc)
                    return ProbeResult(success=False, data=None, tried_path=str(target_path), tried_extension=target_path.suffix, error="cancelled", error_type="cancelled", raw_stderr=None)
                    
                if time.monotonic() > deadline:
                    _kill_proc(proc)
                    return ProbeResult(success=False, data=None, tried_path=str(target_path), tried_extension=target_path.suffix, error="timeout", error_type="timeout", raw_stderr=None)
            
        if ret == 0 and stdout.strip():
            try:
                data = json.loads(stdout)
                if data.get("streams"):
                    return ProbeResult(success=True, data=data, tried_path=str(target_path), tried_extension=target_path.suffix, error=None, error_type=None, raw_stderr=None)
                else:
                    return ProbeResult(success=False, data=None, tried_path=str(target_path), tried_extension=target_path.suffix, error="No streams", error_type="error", raw_stderr=stderr)
            except Exception as e:
                return ProbeResult(success=False, data=None, tried_path=str(target_path), tried_extension=target_path.suffix, error=str(e), error_type="error", raw_stderr=stderr)
        else:
            error_type = "error"
            if "HTTP error 403" in stderr or "Forbidden" in stderr:
                error_type = "forbidden"
            elif "No such file" in stderr:
                error_type = "not_found"
                
            return ProbeResult(success=False, data=None, tried_path=str(target_path), tried_extension=target_path.suffix, error=stderr.strip(), error_type=error_type, raw_stderr=stderr)
            
    except Exception as e:
        if 'proc' in locals():
            _kill_proc(proc)
        return ProbeResult(success=False, data=None, tried_path=str(target_path), tried_extension=target_path.suffix, error=str(e), error_type="error", raw_stderr=None)


def run_ffprobe_sync(
    base_path: Path,
    extensions: List[str],
    timeout: int = 75,
    log_callback: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> ProbeResult:
    def log(msg: str):
        if log_callback: log_callback(msg)

    target_path = None
    parent = base_path.parent
    base_name = base_path.name
    
    for ext in extensions:
        if stop_event and stop_event.is_set():
            return ProbeResult(success=False, data=None, tried_path=None, tried_extension=None, error="cancelled", error_type="cancelled", raw_stderr=None)
        p = base_path.with_suffix(ext)
        try:
            if p.exists():
                target_path = p
                break
        except Exception:
            pass

    if target_path is None:
        try:
            if parent.exists() and parent.is_dir():
                candidates = [f for f in parent.iterdir() if f.is_file() and f.suffix.lower() in extensions]
                if len(candidates) == 1:
                    target_path = candidates[0]
                elif len(candidates) > 1:
                    import re
                    def norm(s): return re.sub(r'\W+', '', s).lower()
                    n_base = norm(base_name)
                    for c in candidates:
                        n_c = norm(c.stem)
                        if n_c.startswith(n_base) or n_base.startswith(n_c):
                            target_path = c
                            break
        except Exception:
            pass

    if target_path is None:
        log(f"  ✗ 未找到对应媒体文件: {base_name}")
        return ProbeResult(
            success=False, data=None,
            tried_path=str(base_path), tried_extension=None,
            error="在目录中未找到对应的视频文件（扩展名或名称不匹配）",
            error_type="not_found", raw_stderr=None
        )

    if stop_event and stop_event.is_set():
        return ProbeResult(success=False, data=None, tried_path=None, tried_extension=None, error="cancelled", error_type="cancelled", raw_stderr=None)

    log(f"  尝试探测: {target_path.name}")
    result = _run_ffprobe_one(target_path, timeout, stop_event, log)

    if result.success:
        log(f"  ✓ 探测成功: {target_path.name} ({target_path.suffix})")
        return result

    if result.error_type == "cancelled":
        return result

    return result


def probe_with_retry(
    base_path: Path,
    extensions: List[str],
    timeout: int,
    max_retries: int,
    retry_delay: float,
    forbidden_retry_delay: float,
    log_callback: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> ProbeResult:
    def log(msg: str):
        if log_callback: log_callback(msg)

    last_result = None
    for attempt in range(1, max_retries + 1):
        if stop_event and stop_event.is_set():
            if last_result: return last_result
            return ProbeResult(success=False, data=None, tried_path=None, tried_extension=None, error="cancelled", error_type="cancelled", raw_stderr=None)
            
        if attempt > 1:
            log(f"  ▷ 第 {attempt}/{max_retries} 次重试...")
            
        result = run_ffprobe_sync(base_path, extensions, timeout, log, stop_event)
        last_result = result
        
        if result.success:
            return result
            
        if result.error_type == "cancelled":
            return result
            
        if result.error_type == "not_found":
            return result
            
        if attempt < max_retries:
            delay = forbidden_retry_delay if result.error_type == "forbidden" else retry_delay
            reason = "403 Forbidden" if result.error_type == "forbidden" else "超时/错误"
            log(f"  {reason}，等待 {delay}s 后重试...")
            
            if stop_event:
                if stop_event.wait(delay):
                    return ProbeResult(success=False, data=None, tried_path=None, tried_extension=None, error="cancelled", error_type="cancelled", raw_stderr=None)
            else:
                time.sleep(delay)
                
    if last_result and not last_result.success:
        log(f"  达到最大重试次数 ({max_retries})，放弃")
        
    return last_result
