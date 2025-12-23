import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class CmdResult:
    cmd: List[str]
    returncode: int


TOOL_BIN: dict = {}


def _run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[dict] = None, check: bool = True) -> CmdResult:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}")
    return CmdResult(cmd=cmd, returncode=p.returncode)


def _run_capture(cmd: List[str], cwd: Optional[Path] = None, env: Optional[dict] = None, check: bool = True) -> str:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}")
    return p.stdout


def _exe(name: str) -> str:
    return TOOL_BIN.get(name, name)


def _which_or_raise(label: str, override: Optional[str] = None) -> str:
    if override:
        if Path(override).exists():
            return override
        raise RuntimeError(f"Tool path does not exist for {label}: {override}")

    p = shutil.which(label)
    if not p:
        raise RuntimeError(
            f"Missing required tool in PATH: {label}. "
            f"Please install it or add it to PATH (or pass --{label}-bin)."
        )
    return p


def _ensure_tools(args: argparse.Namespace) -> dict:
    """Return resolved tool executables.

    Notes:
    - docker/kubectl/minikube are always required.
    - mvn/npm/node are required only when we need to build artifacts (not --skip-build and not --only-apply).
    """
    tools = {
        "docker": _which_or_raise("docker", getattr(args, "docker_bin", None)),
        "kubectl": _which_or_raise("kubectl", getattr(args, "kubectl_bin", None)),
        "minikube": _which_or_raise("minikube", getattr(args, "minikube_bin", None)),
    }

    need_build = (not args.skip_build) and (not args.only_apply)
    if need_build:
        tools.update(
            {
                "mvn": _which_or_raise("mvn", getattr(args, "mvn_bin", None)),
                "node": _which_or_raise("node", getattr(args, "node_bin", None)),
                "npm": _which_or_raise("npm", getattr(args, "npm_bin", None)),
            }
        )
    return tools


def _ensure_base_images() -> None:
    """Ensure base images exist locally.

    If your Docker is configured with an unreachable registry mirror (e.g. returning 403),
    pulling will fail. In that case we raise a clear error so the user can fix Docker registry mirrors.
    """
    base_images = [
        "eclipse-temurin:17-jre",
        "nginx:latest",
        "redis:7",
        "mysql:5.7",
        "nacos/nacos-server:latest",
    ]

    def image_exists(img: str) -> bool:
        p = subprocess.run([_exe("docker"), "image", "inspect", img], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p.returncode == 0

    missing = [i for i in base_images if not image_exists(i)]
    if not missing:
        return

    # Try pull sequentially for clearer error output.
    for img in missing:
        try:
            _run([_exe("docker"), "pull", img], check=True)
        except Exception as e:
            raise RuntimeError(
                "Failed to pull required base image: "
                + img
                + "\n\n"
                + "This is usually caused by Docker registry mirror/网络限制 (e.g. mirror returns 403).\n"
                + "Fix options:\n"
                + "1) Disable/replace Docker registry mirrors in Docker Desktop settings\n"
                + "2) Configure a working mirror\n"
                + "3) Manually `docker pull {img}` until success\n\n"
                + f"Original error: {e}"
            )


def _build_backend_jars() -> None:
    # Build all modules; jars are required by docker/*/dockerfile via _copy_assets.
    _run([_exe("mvn"), "-DskipTests", "package"], cwd=_repo_root(), check=True)


def _build_frontend_dist() -> None:
    ui_dir = _repo_root() / "ruoyi-ui"
    pkg = ui_dir / "package.json"
    if not pkg.exists():
        raise RuntimeError(f"ruoyi-ui not found: {pkg}")

    # Use npm install for maximum compatibility (users may not have lockfile/ci).
    _run([_exe("npm"), "install"], cwd=ui_dir, check=True)
    _run([_exe("npm"), "run", "build:prod"], cwd=ui_dir, check=True)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _docker_dir() -> Path:
    return _repo_root() / "docker"


def _k8s_yaml() -> Path:
    return Path(__file__).resolve().parent / "k8s" / "all.yaml"


def _copy_assets() -> None:
    # docker/copy.sh is linux shell; on windows we implement the same copy in python
    repo = _repo_root()
    dkr = _docker_dir()

    sql_src1 = repo / "sql" / "ry_20250523.sql"
    sql_src2 = repo / "sql" / "ry_config_20250902.sql"
    mysql_db = dkr / "mysql" / "db"
    mysql_db.mkdir(parents=True, exist_ok=True)

    if sql_src1.exists():
        (mysql_db / sql_src1.name).write_bytes(sql_src1.read_bytes())
    if sql_src2.exists():
        (mysql_db / sql_src2.name).write_bytes(sql_src2.read_bytes())

    # ui dist
    ui_dist = repo / "ruoyi-ui" / "dist"
    nginx_dist = dkr / "nginx" / "html" / "dist"
    if ui_dist.exists():
        if nginx_dist.exists():
            # keep existing, but ensure directory exists
            pass
        nginx_dist.mkdir(parents=True, exist_ok=True)
        # copy tree
        for root, dirs, files in os.walk(ui_dist):
            rel = Path(root).relative_to(ui_dist)
            (nginx_dist / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                src = Path(root) / f
                dst = nginx_dist / rel / f
                dst.write_bytes(src.read_bytes())

    # jars
    jar_map = {
        "ruoyi-gateway": (repo / "ruoyi-gateway" / "target" / "ruoyi-gateway.jar", dkr / "ruoyi" / "gateway" / "jar"),
        "ruoyi-auth": (repo / "ruoyi-auth" / "target" / "ruoyi-auth.jar", dkr / "ruoyi" / "auth" / "jar"),
        "ruoyi-visual-monitor": (
            repo / "ruoyi-visual" / "ruoyi-monitor" / "target" / "ruoyi-visual-monitor.jar",
            dkr / "ruoyi" / "visual" / "monitor" / "jar",
        ),
        "ruoyi-modules-system": (
            repo / "ruoyi-modules" / "ruoyi-system" / "target" / "ruoyi-modules-system.jar",
            dkr / "ruoyi" / "modules" / "system" / "jar",
        ),
        "ruoyi-modules-file": (
            repo / "ruoyi-modules" / "ruoyi-file" / "target" / "ruoyi-modules-file.jar",
            dkr / "ruoyi" / "modules" / "file" / "jar",
        ),
        "ruoyi-modules-job": (
            repo / "ruoyi-modules" / "ruoyi-job" / "target" / "ruoyi-modules-job.jar",
            dkr / "ruoyi" / "modules" / "job" / "jar",
        ),
        "ruoyi-modules-gen": (
            repo / "ruoyi-modules" / "ruoyi-gen" / "target" / "ruoyi-modules-gen.jar",
            dkr / "ruoyi" / "modules" / "gen" / "jar",
        ),
    }

    missing = []
    for name, (src, dst_dir) in jar_map.items():
        if not src.exists():
            missing.append(str(src))
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / src.name).write_bytes(src.read_bytes())

    if missing:
        raise RuntimeError(
            "Missing jar build outputs. Please build the project first (e.g. mvn -DskipTests package). Missing:\n"
            + "\n".join(missing)
        )


def _write_patched_sql(tmp_dir: Path) -> List[Path]:
    """Prepare SQL files for mysql init.

    We patch Nacos config SQL so that in-cluster services can start:
    - mysql host: ruoyi-mysql
    - redis host: ruoyi-redis
    - nacos addr: ruoyi-nacos
    """
    repo = _repo_root()
    sql_dir = repo / "sql"
    candidates = [
        sql_dir / "ry_20250523.sql",
        sql_dir / "ry_config_20250902.sql",
    ]
    out_files: List[Path] = []
    for src in candidates:
        if not src.exists():
            continue
        content = src.read_text(encoding="utf-8", errors="ignore")
        if "ry_config" in src.name or "config" in src.name:
            content = content.replace("jdbc:mysql://localhost:3306/ry-cloud", "jdbc:mysql://ruoyi-mysql:3306/ry-cloud")
            content = content.replace("server-addr: 127.0.0.1:8848", "server-addr: ruoyi-nacos:8848")
            content = content.replace("server-addr: 127.0.0.1:8718", "server-addr: ruoyi-nacos:8718")
            content = content.replace("host: localhost\n    port: 6379", "host: ruoyi-redis\n    port: 6379")
            content = content.replace("host: localhost", "host: ruoyi-redis")
            content = content.replace("host: 127.0.0.1", "host: ruoyi-redis")
            content = content.replace("jdbc:mysql://localhost:3306/ry-config", "jdbc:mysql://ruoyi-mysql:3306/ry-config")
            content = content.replace("classpath:mapper/**/*.xml", "classpath*:mapper/**/*.xml")

        dst = tmp_dir / src.name
        dst.write_text(content, encoding="utf-8")
        out_files.append(dst)
    if not out_files:
        raise RuntimeError("No SQL files found under ./sql to initialize mysql")
    return out_files


def _apply_configmaps(ns: str) -> None:
    # Ensure namespace exists before creating configmaps.
    # DO NOT apply the whole manifest here, otherwise mysql may start before SQL configmap is populated.
    ns_yaml = _run_capture([_exe("kubectl"), "create", "ns", ns, "--dry-run=client", "-o", "yaml"], check=False)
    if ns_yaml.strip():
        subprocess.run(
            [_exe("kubectl"), "apply", "-f", "-"],
            input=ns_yaml,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

    repo_docker = _docker_dir()
    nacos_props = repo_docker / "nacos" / "conf" / "application.properties"
    if not nacos_props.exists():
        raise RuntimeError(f"Missing nacos config file: {nacos_props}")

    with tempfile.TemporaryDirectory(prefix="ruoyi-minikube-") as td:
        tmp_dir = Path(td)
        sql_files = _write_patched_sql(tmp_dir)

        # mysql init configmap (multiple sql files)
        cmd = [
            _exe("kubectl"),
            "-n",
            ns,
            "create",
            "configmap",
            "ruoyi-mysql-init",
        ]
        for f in sql_files:
            cmd.append(f"--from-file={f.name}={str(f)}")
        cmd += ["--dry-run=client", "-o", "yaml"]
        yaml_out = _run_capture(cmd, check=True)
        subprocess.run(
            [_exe("kubectl"), "apply", "-f", "-"],
            input=yaml_out,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

        # nacos application.properties configmap
        cmd2 = [
            _exe("kubectl"),
            "-n",
            ns,
            "create",
            "configmap",
            "ruoyi-nacos-conf",
            f"--from-file=application.properties={str(nacos_props)}",
            "--dry-run=client",
            "-o",
            "yaml",
        ]
        yaml_out2 = _run_capture(cmd2, check=True)
        subprocess.run(
            [_exe("kubectl"), "apply", "-f", "-"],
            input=yaml_out2,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )


def _build_images_parallel(images: List[str], docker_compose_yml: Path) -> None:
    # IMPORTANT:
    # docker-compose.yml in this repo sets some base images as official names (e.g. mysql:5.7, nginx).
    # If we run `docker compose build`, it may overwrite/tag official image names locally.
    # Here we always build with explicit tags matching our K8s manifests.
    repo_docker = _docker_dir()

    build_plan = {
        # ui
        "ruoyi-ui:latest": (repo_docker / "nginx", repo_docker / "nginx" / "dockerfile"),
        # apps
        "ruoyi-gateway:jre17-1": (repo_docker / "ruoyi" / "gateway", repo_docker / "ruoyi" / "gateway" / "dockerfile"),
        "ruoyi-auth:jre17-1": (repo_docker / "ruoyi" / "auth", repo_docker / "ruoyi" / "auth" / "dockerfile"),
        "ruoyi-modules-system:jre17-1": (repo_docker / "ruoyi" / "modules" / "system", repo_docker / "ruoyi" / "modules" / "system" / "dockerfile"),
        "ruoyi-modules-gen:jre17-1": (repo_docker / "ruoyi" / "modules" / "gen", repo_docker / "ruoyi" / "modules" / "gen" / "dockerfile"),
        "ruoyi-modules-job:jre17-1": (repo_docker / "ruoyi" / "modules" / "job", repo_docker / "ruoyi" / "modules" / "job" / "dockerfile"),
        "ruoyi-modules-file:jre17-1": (repo_docker / "ruoyi" / "modules" / "file", repo_docker / "ruoyi" / "modules" / "file" / "dockerfile"),
        "ruoyi-visual-monitor:jre17-1": (repo_docker / "ruoyi" / "visual" / "monitor", repo_docker / "ruoyi" / "visual" / "monitor" / "dockerfile"),
    }

    def build_one(img: str) -> CmdResult:
        if img not in build_plan:
            raise RuntimeError(f"No build plan for image: {img}")
        context_dir, dockerfile = build_plan[img]
        return _run([
            _exe("docker"),
            "build",
            "-t",
            img,
            "-f",
            str(dockerfile),
            str(context_dir),
        ])

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(images))) as ex:
        futs = [ex.submit(build_one, s) for s in images]
        for f in concurrent.futures.as_completed(futs):
            f.result()


def _minikube_load_parallel(images: List[str]) -> None:
    def load_one(img: str) -> CmdResult:
        return _run([_exe("minikube"), "image", "load", img])

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(images))) as ex:
        futs = [ex.submit(load_one, i) for i in images]
        for f in concurrent.futures.as_completed(futs):
            f.result()


def _kubectl_apply(ns: str) -> None:
    _run([_exe("kubectl"), "apply", "-f", str(_k8s_yaml())])


def _wait_rollout(ns: str, deployments: Iterable[str], timeout_sec: int = 600) -> None:
    start = time.time()
    for d in deployments:
        left = max(30, timeout_sec - int(time.time() - start))
        _run([_exe("kubectl"), "-n", ns, "rollout", "status", f"deploy/{d}", f"--timeout={left}s"], check=True)


def _get_single_pod_name(ns: str, label_selector: str) -> str:
    out = _run_capture([
        _exe("kubectl"),
        "-n",
        ns,
        "get",
        "pod",
        "-l",
        label_selector,
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ])
    name = out.strip()
    if not name:
        raise RuntimeError(f"No pod found for selector: {label_selector} in ns={ns}")
    return name


def _mysql_exec(ns: str, pod: str, shell_cmd: str, check: bool = True) -> CmdResult:
    return _run([
        _exe("kubectl"),
        "-n",
        ns,
        "exec",
        pod,
        "--",
        "sh",
        "-lc",
        shell_cmd,
    ], check=check)


def _mysql_sql(ns: str, pod: str, database: str, sql: str, check: bool = True) -> CmdResult:
    # Run a single SQL statement (safe quoting for our fixed strings)
    cmd = [
        _exe("kubectl"),
        "-n",
        ns,
        "exec",
        pod,
        "--",
        "mysql",
        "--default-character-set=utf8mb4",
        "-uroot",
        "-ppassword",
        f"--database={database}",
        "-e",
        sql,
    ]
    return _run(cmd, check=check)


def _fix_ry_config_redis_host(ns: str) -> None:
    """Fix common misconfig where services try to connect Redis at localhost inside K8s.

    We patch ry-config.config_info in-place to replace:
    - host: localhost
    - host: 127.0.0.1
    with:
    - host: ruoyi-redis

    Why:
    - Some services (notably ruoyi-system) may pull dataId=ruoyi-system or ruoyi-system.yml (not -dev),
      so patching only *-dev.yml is not sufficient.
    """
    mysql_pod = _get_single_pod_name(ns, "app=ruoyi-mysql")

    # Patch all ruoyi-related dataIds to be robust.
    # Avoid changing unrelated configs by restricting to ruoyi-%
    _mysql_sql(
        ns,
        mysql_pod,
        "ry-config",
        "UPDATE config_info "
        "SET content=REPLACE(REPLACE(content,'host: localhost','host: ruoyi-redis'),'host: 127.0.0.1','host: ruoyi-redis'), "
        "    gmt_modified=NOW() "
        "WHERE data_id LIKE 'ruoyi-%';",
        check=True,
    )


def _ensure_mysql_initialized(ns: str) -> None:
    # Make sure mysql is ready first.
    _wait_rollout(ns, ["ruoyi-mysql"], timeout_sec=600)

    mysql_pod = _get_single_pod_name(ns, "app=ruoyi-mysql")

    # ---- Nacos config DB (ry-config) ----
    # NOTE: It's not enough to check the database exists; we must ensure core tables exist.
    # Otherwise Nacos will crash or behave incorrectly.
    db_check_cmd = "mysql -uroot -ppassword -N -e \"SHOW DATABASES LIKE 'ry-config';\""
    out = _run_capture([
        _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", db_check_cmd
    ], check=False).strip()

    # Import init SQL files explicitly (idempotent for CREATE DATABASE IF EXISTS patterns)
    # Note: files are mounted from ConfigMap into /docker-entrypoint-initdb.d
    _mysql_exec(ns, mysql_pod, "ls -la /docker-entrypoint-initdb.d", check=True)

    # Ensure databases exist, then import into the right database explicitly.
    _mysql_exec(
        ns,
        mysql_pod,
        "mysql -uroot -ppassword -e \"CREATE DATABASE IF NOT EXISTS `ry-config` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;\"",
        check=True,
    )
    _mysql_exec(
        ns,
        mysql_pod,
        "mysql --default-character-set=utf8mb4 -uroot -ppassword ry-config < /docker-entrypoint-initdb.d/ry_config_20250902.sql",
        check=True,
    )

    # Verify ry-config core table exists (Nacos config_info)
    cfg_table_check_cmd = "mysql --default-character-set=utf8mb4 -uroot -ppassword -N -D ry-config -e \"SHOW TABLES LIKE 'config_info';\""
    cfg = _run_capture([
        _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", cfg_table_check_cmd
    ], check=False).strip()
    if cfg != "config_info":
        # Retry import with --force once (handles partial imports)
        _mysql_exec(
            ns,
            mysql_pod,
            "mysql --default-character-set=utf8mb4 --force -uroot -ppassword ry-config < /docker-entrypoint-initdb.d/ry_config_20250902.sql",
            check=False,
        )
        cfg2 = _run_capture([
            _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", cfg_table_check_cmd
        ], check=False).strip()
        if cfg2 != "config_info":
            raise RuntimeError("MySQL init incomplete: table ry-config.config_info not found after importing ry_config_20250902.sql")

    _mysql_exec(
        ns,
        mysql_pod,
        "mysql -uroot -ppassword -e \"CREATE DATABASE IF NOT EXISTS `ry-cloud` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;\"",
        check=True,
    )
    _mysql_exec(
        ns,
        mysql_pod,
        "mysql --default-character-set=utf8mb4 -uroot -ppassword ry-cloud < /docker-entrypoint-initdb.d/ry_20250523.sql",
        check=False,
    )

    # Verify key table exists in ry-cloud for ruoyi modules
    # (avoid service CrashLoop with 'Table ry-cloud.sys_config doesn't exist')
    # Avoid backticks (shell command substitution) by selecting DB via -D.
    table_check_cmd = "mysql --default-character-set=utf8mb4 -uroot -ppassword -D ry-cloud -N -e \"SHOW TABLES LIKE 'sys_config';\""
    t = _run_capture([
        _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", table_check_cmd
    ], check=False).strip()
    if t != "sys_config":
        # Retry import once in case of transient failure
        _mysql_exec(
            ns,
            mysql_pod,
            "mysql --default-character-set=utf8mb4 -uroot -ppassword ry-cloud < /docker-entrypoint-initdb.d/ry_20250523.sql",
            check=False,
        )
        t2 = _run_capture([
            _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", table_check_cmd
        ], check=False).strip()
        if t2 != "sys_config":
            raise RuntimeError("MySQL init incomplete: table ry-cloud.sys_config not found after importing ry_20250523.sql")

    # Also verify sys_job exists (ruoyi-job depends on it)
    job_check_cmd = "mysql --default-character-set=utf8mb4 -uroot -ppassword -D ry-cloud -N -e \"SHOW TABLES LIKE 'sys_job';\""
    j = _run_capture([
        _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", job_check_cmd
    ], check=False).strip()
    if j != "sys_job":
        _mysql_exec(
            ns,
            mysql_pod,
            "mysql --default-character-set=utf8mb4 --force -uroot -ppassword ry-cloud < /docker-entrypoint-initdb.d/ry_20250523.sql",
            check=False,
        )
        j2 = _run_capture([
            _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", job_check_cmd
        ], check=False).strip()
        if j2 != "sys_job":
            raise RuntimeError("MySQL init incomplete: table ry-cloud.sys_job not found after importing ry_20250523.sql")

    # Re-check database still exists (paranoia check)
    out2 = _run_capture([
        _exe("kubectl"), "-n", ns, "exec", mysql_pod, "--", "sh", "-lc", db_check_cmd
    ], check=False).strip()
    if out2 != "ry-config":
        raise RuntimeError("MySQL init failed: ry-config database not found after importing SQL")


def _restart_and_wait(ns: str, deployment: str, timeout_sec: int = 600) -> None:
    _run([_exe("kubectl"), "-n", ns, "rollout", "restart", f"deploy/{deployment}"])
    _wait_rollout(ns, [deployment], timeout_sec=timeout_sec)


def _print_access(ns: str) -> None:
    # Print url from minikube service
    _run([_exe("minikube"), "service", "-n", ns, "ruoyi-nginx", "--url"], check=False)


def _cleanup(ns: str) -> None:
    _run([_exe("kubectl"), "delete", "ns", ns, "--ignore-not-found=true"], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy RuoYi-Cloud to minikube")
    parser.add_argument("--namespace", default="ruoyi")
    parser.add_argument("--skip-build", action="store_true", help="Skip docker build")
    parser.add_argument("--only-apply", action="store_true", help="Only kubectl apply and wait")
    parser.add_argument("--cleanup", action="store_true", help="Delete namespace and exit")
    parser.add_argument("--docker-bin", default=None)
    parser.add_argument("--kubectl-bin", default=None)
    parser.add_argument("--minikube-bin", default=None)
    parser.add_argument("--mvn-bin", default=None)
    parser.add_argument("--node-bin", default=None)
    parser.add_argument("--npm-bin", default=None)
    args = parser.parse_args()

    global TOOL_BIN
    TOOL_BIN = _ensure_tools(args)
    _ensure_base_images()

    if args.cleanup:
        _cleanup(args.namespace)
        return 0

    if not _k8s_yaml().exists():
        raise RuntimeError(f"k8s manifest not found: {_k8s_yaml()}")

    # Ensure namespace in yaml matches
    if args.namespace != "ruoyi":
        raise RuntimeError("This initial version only supports namespace 'ruoyi' (hardcoded in yaml).")

    if not args.only_apply:
        # build backend and frontend in parallel for speed
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            fut_backend = ex.submit(_build_backend_jars)
            fut_frontend = ex.submit(_build_frontend_dist)
            fut_backend.result()
            fut_frontend.result()

        _copy_assets()

        if not args.skip_build:
            docker_compose = _docker_dir() / "docker-compose.yml"
            imgs_to_build = [
                "ruoyi-gateway:jre17-1",
                "ruoyi-auth:jre17-1",
                "ruoyi-modules-system:jre17-1",
                "ruoyi-modules-gen:jre17-1",
                "ruoyi-modules-job:jre17-1",
                "ruoyi-modules-file:jre17-1",
                "ruoyi-visual-monitor:jre17-1",
                "ruoyi-ui:latest",
            ]
            _build_images_parallel(imgs_to_build, docker_compose)

        # Ensure images are available in minikube runtime (safe even if already there)
        imgs = [
            "ruoyi-gateway:jre17-1",
            "ruoyi-auth:jre17-1",
            "ruoyi-modules-system:jre17-1",
            "ruoyi-modules-gen:jre17-1",
            "ruoyi-modules-job:jre17-1",
            "ruoyi-modules-file:jre17-1",
            "ruoyi-visual-monitor:jre17-1",
            "ruoyi-ui:latest",
        ]
        _minikube_load_parallel(imgs)

    # Create/Update configmaps for mysql init and nacos config (must happen before pods start)
    _apply_configmaps(args.namespace)

    _kubectl_apply(args.namespace)

    # Ensure mysql schema/config DB are initialized, then restart nacos (depends on ry-config)
    _ensure_mysql_initialized(args.namespace)

    # Fix common localhost Redis misconfig in ry-config before restarting services.
    _fix_ry_config_redis_host(args.namespace)

    _restart_and_wait(args.namespace, "ruoyi-nacos", timeout_sec=900)

    deployments = [
        "ruoyi-mysql",
        "ruoyi-redis",
        "ruoyi-nacos",
        "ruoyi-gateway",
        "ruoyi-auth",
        "ruoyi-system",
        "ruoyi-gen",
        "ruoyi-job",
        "ruoyi-file",
        "ruoyi-monitor",
        "ruoyi-nginx",
    ]
    _wait_rollout(args.namespace, deployments, timeout_sec=900)
    _print_access(args.namespace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
