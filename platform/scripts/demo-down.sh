#!/usr/bin/env bash
# demo-down.sh —— demo-up 的对手戏：把演示环境收干净。
#
# 顺序：先按 pid 文件优雅停掉五个本机应用进程，再停容器，最后做残留检查。
#
# 绝不删卷。三个命名卷里装着 20000 文档的 ES 索引、11 个策略版本与完整审计流，
# 销毁了重建不出来，因此这里只用 `docker compose stop`，永远不传 -v / --volumes。
# 真要清空请显式走 make clean-local（那个脚本才是干这件事的）。
#
# 幂等：什么都没在跑时执行本脚本不报错，正常退出。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PID_DIR="$PROJECT_DIR/.runtime/pid"
# 归属校验的边界：进程 cwd 必须落在仓库内，否则判定 pid 已被系统复用。
REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/.runtime/logs"

# 与 demo-up 相同的服务表，停止顺序取反（先停依赖方，再停被依赖方）。
SERVICES=(operations-console storefront commerce search-service ai-adapter)
PORTS=(3001               3000       9000     8080           8000)

TERM_TIMEOUT=25   # 收到 SIGTERM 后允许优雅退出的秒数（mvn/medusa 关得比较慢）

log()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

# --------------------------------------------------------------------------
# 进程识别：宁可少停，也不误杀
# --------------------------------------------------------------------------

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

pid_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

listeners_on_port() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u || true
}

# 监听 <port> **且** cwd 位于本项目目录之下的进程 pid。
#
# 这是 pid 文件失效时唯一使用的兜底匹配，两个条件必须同时成立：
#   1) 该进程确实在监听这个端口；
#   2) 该进程的 cwd 在 $PROJECT_DIR 之内。
#
# 为什么这样就不会误杀：
#   - 只按命令名匹配（node / java / python / "next dev"）是危险的——这些名字满机器都是，
#     用户另一个项目的 Next.js 会被一并杀掉，所以这里完全不用命令名做判据；
#   - 只按端口匹配也不够——3000 是最容易撞车的端口；
#   - 端口 + cwd 同时命中，意味着这个进程是从本仓库目录里启动的，属于本环境；
#   - Docker 的端口转发进程 cwd 不在本项目下，因此永远不会被命中，卷和容器不受影响。
owned_pids_on_port() {
  local port="$1" pid cwd
  for pid in $(listeners_on_port "$port"); do
    cwd="$(pid_cwd "$pid")"
    case "$cwd" in
      "$PROJECT_DIR"|"$PROJECT_DIR"/*) printf '%s\n' "$pid" ;;
    esac
  done
}

# 等某个 pid 消失，最多 $TERM_TIMEOUT 秒。
wait_gone() {
  local pid="$1" waited=0
  while [ "$waited" -lt "$TERM_TIMEOUT" ]; do
    pid_alive "$pid" || return 0
    sleep 1
    waited=$((waited + 1))
  done
  if pid_alive "$pid"; then
    return 1
  fi
  return 0
}

# --------------------------------------------------------------------------
# 停应用进程
# --------------------------------------------------------------------------

# 主路径：pid 文件有效时按进程组停。
# demo-up 用 `set -m` 让每个服务拿到独立进程组（pgid == 启动时的 pid），所以
# kill -TERM -<pgid> 能精确停掉 mvn→java、npm→next、uvicorn→reload worker 整棵子树，
# 而进程组是启动时创建的，外部进程不可能混进来。
stop_by_pidfile() {
  # 注意：local 的多个赋值不能互相引用。bash 在执行任何赋值之前先展开整条 local 语句的
  # 所有词，所以写成 `local name="$1" pidfile="$PID_DIR/$name.pid"` 时，$name 取到的是
  # **调用者作用域**里的同名变量（动态作用域），不是刚赋的 "$1"。拆成两条才正确。
  local name="$1"
  local pidfile="$PID_DIR/$name.pid"
  local pid pgid owner_cwd
  [ -f "$pidfile" ] || return 1
  pid="$(tr -dc '0-9' < "$pidfile")"
  if ! pid_alive "$pid"; then
    rm -f "$pidfile"
    return 1   # pid 文件失效，交给兜底路径
  fi
  # 归属校验：pid 会被系统回收复用。只确认"这个号还活着"不足以证明它还是我们起的那个进程——
  # demo-up 与 demo-down 之间可能隔了几小时，期间原进程退出、号被别人用上完全可能。
  # 这里要求该进程的工作目录仍落在本仓库内；对不上就放弃按 pid 停，交给端口+cwd 双重命中的兜底路径。
  owner_cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
  case "$owner_cwd" in
    "$REPO_ROOT"*) : ;;
    *)
      log "    ${name}: pid ${pid} 的 cwd 为 ${owner_cwd:-未知}，不在本仓库内——判定为 pid 已被复用，不停它"
      rm -f "$pidfile"
      return 1
      ;;
  esac
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
  if [ -n "$pgid" ] && [ "$pgid" = "$pid" ]; then
    # 只有确认 pgid == pid（即该进程本身就是 demo-up 创建的进程组组长）才按组停，
    # 否则退化成只停这个 pid——绝不对一个我们没创建的进程组群发信号。
    kill -TERM -"$pgid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
  if ! wait_gone "$pid"; then
    log "    ${name}: SIGTERM 后 ${TERM_TIMEOUT}s 未退出，升级为 SIGKILL"
    if [ -n "$pgid" ] && [ "$pgid" = "$pid" ]; then
      kill -KILL -"$pgid" 2>/dev/null || true
    else
      kill -KILL "$pid" 2>/dev/null || true
    fi
    sleep 1
  fi
  rm -f "$pidfile"
  printf '%-20s stopped   (pid %s)\n' "$name" "$pid"
  return 0
}

# 兜底路径：pid 文件丢失/失效，但端口上还有本项目起的进程。
# 这里只停「端口 + cwd」双重命中的那个进程本身，不按进程组群发：
# 没有 pid 文件就说明这棵进程树不是本脚本创建的（可能是手工 make dev-* 起的），
# 此时最小动作 = 只终止真正占着端口的那个进程，这是能保证不误伤的最保守做法。
stop_by_port() {
  local name="$1" port="$2" pids pid left
  pids="$(owned_pids_on_port "$port")"
  if [ -z "$pids" ]; then
    left="$(listeners_on_port "$port")"
    if [ -n "$left" ]; then
      # 端口有人占，但 cwd 不在本项目下（多半是容器转发或别的项目）→ 报告，不动它。
      printf '%-20s skipped   (端口 %s 被非本项目进程占用，未触碰)\n' "$name" "$port"
      lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed 's/^/    /' || true
      return 0
    fi
    printf '%-20s not-running\n' "$name"
    return 0
  fi
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
    if ! wait_gone "$pid"; then
      kill -KILL "$pid" 2>/dev/null || true
      sleep 1
    fi
    printf '%-20s stopped   (pid %s, 经端口+工作目录双重确认)\n' "$name" "$pid"
  done
}

stop_apps() {
  step "应用进程"
  local i name port
  i=0
  while [ "$i" -lt "${#SERVICES[@]}" ]; do
    name="${SERVICES[$i]}"; port="${PORTS[$i]}"
    if ! stop_by_pidfile "$name"; then
      stop_by_port "$name" "$port"
    fi
    i=$((i + 1))
  done
}

# --------------------------------------------------------------------------
# 停容器
# --------------------------------------------------------------------------

stop_containers() {
  step "容器"
  local running
  running="$(docker compose ps --services --filter status=running 2>/dev/null || true)"
  if [ -z "$running" ]; then
    log "没有运行中的项目容器。"
  else
    log "停止：$(printf '%s ' $running)"
  fi
  # 只 stop，不 down、不 -v：容器可以重建，卷里的数据重建不出来。
  docker compose stop
}

# --------------------------------------------------------------------------
# 残留检查
# --------------------------------------------------------------------------

report_residual() {
  step "残留检查"
  local i port name pids leftover=0 project_name

  i=0
  while [ "$i" -lt "${#SERVICES[@]}" ]; do
    name="${SERVICES[$i]}"; port="${PORTS[$i]}"
    pids="$(listeners_on_port "$port")"
    if [ -n "$pids" ]; then
      leftover=1
      printf '端口 %-5s 仍被占用：pid %s\n' "$port" "$(printf '%s ' $pids)"
      lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed 's/^/    /' || true
    fi
    i=$((i + 1))
  done
  if [ "$leftover" -eq 0 ]; then
    log "应用端口 3000/3001/8000/8080/9000 均已释放。"
  fi

  local still
  still="$(docker compose ps --services --filter status=running 2>/dev/null || true)"
  if [ -n "$still" ]; then
    log "仍在运行的容器：$(printf '%s ' $still)"
  else
    log "项目容器均已停止（容器保留，未删除）。"
  fi

  # 明确把卷列出来，作为「没删数据」的证据。
  # 这里只从 .env 里取项目名（不 source 整个文件，避免把密钥带进本进程环境）。
  local volumes
  project_name="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env 2>/dev/null | head -1 | tr -d "\"' ")"
  if [ -z "$project_name" ]; then
    project_name="commerce-searchops-lab"
  fi
  volumes="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep "^${project_name}_" || true)"
  log "数据卷（保留，本脚本从不传 -v / --volumes）："
  if [ -n "$volumes" ]; then
    printf '%s\n' "$volumes" | sed 's/^/    /'
  else
    log "    (未找到 ${project_name}_ 开头的卷)"
  fi

  # 只报告、不动手：命令行里带着本项目绝对路径的遗留进程。
  #
  # 为什么需要这一段：兜底路径只终止「真正占着端口」的那个进程，像 npm / medusa develop
  # 这样的父进程可能还活着。此时端口已经释放，上面按端口做的检查看不见它们，
  # 于是"干净"是个假象。这里把它们如实摆出来，但**不代杀**——没有 pid 文件就说明
  # 这棵进程树不是本脚本创建的，杀不杀由人来定。
  local pstable strays
  pstable="$(ps -eo pid=,command= 2>/dev/null || true)"
  # 先把 ps 快照存下来再过滤：否则 grep/awk 自己的命令行里也含项目路径，会自己匹配自己。
  strays="$(printf '%s\n' "$pstable" | grep -F "$PROJECT_DIR/" | awk -v self="$$" -v parent="$PPID" '$1 != self && $1 != parent' || true)"
  if [ -n "$strays" ]; then
    log "仍有命令行指向本项目的进程（未触碰，请自行确认）："
    printf '%s\n' "$strays" | cut -c1-140 | sed 's/^/    /'
  fi

  # pid 文件应当已经清空；日志留着供事后排查。
  if ls "$PID_DIR"/*.pid >/dev/null 2>&1; then
    log "残留 pid 文件：$(ls "$PID_DIR"/*.pid)"
  else
    log "pid 文件已清理。日志保留在 $LOG_DIR/"
  fi
}

main() {
  stop_apps
  stop_containers
  report_residual
  printf '\n'
}

main "$@"
