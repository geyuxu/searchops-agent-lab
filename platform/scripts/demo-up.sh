#!/usr/bin/env bash
# demo-up.sh —— 一条命令把「开发形态」的演示环境完整拉起来。
#
# 开发形态 = 三个有状态基础设施跑容器（postgres/redis/elasticsearch）
#            + 五个应用跑本机进程（search-service/ai-adapter/commerce/storefront/console）。
# 它与 make up（八个服务全部容器化，用于演示与 CI）并存而不是取代它：容器化应用会让
# 改一行 Java 就得重打 Maven 镜像、Next.js 也没有热重载，所以日常开发走这条路径。
#
# 手工等价物是开五个终端分别跑 make dev-ai / dev-search / dev-commerce / dev-store /
# dev-console。本脚本把它们收拢成一条命令，并补上「等就绪 + 失败定位」这两件手工做不好的事。
#
# 幂等：已经在跑的服务不会被重复启动，重复执行本脚本是安全的。
# 收干净用 make demo-down。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

RUNTIME_DIR="$PROJECT_DIR/.runtime"
PID_DIR="$RUNTIME_DIR/pid"
LOG_DIR="$RUNTIME_DIR/logs"

INFRA="postgres redis elasticsearch"
INFRA_TIMEOUT=180

# 五个应用。四组数组按下标一一对应（macOS 自带 bash 是 3.2，没有关联数组）。
# 顺序即启动顺序，与 docker-compose.yml 里的 depends_on 一致：
# ai-adapter → search-service → commerce → storefront/operations-console。
SERVICES=(ai-adapter        search-service               commerce  storefront   operations-console)
PORTS=(8000                 8080                         9000      3000         3001)
HEALTH=(/ai/health          /actuator/health/readiness   /health   /api/health  /api/health)
# 每个服务的就绪预算（秒）。Maven 冷编译和 medusa develop 都可能跑到分钟级；
# Next.js 的 /api/health 是被探测请求本身触发编译的，所以也不能给得太紧。
TIMEOUTS=(90                420                          420       240          240)
LABELS=("AI 适配器"        "搜索服务"                   "商城服务" "商城前台"   "运营后台")

JAVA21_HOME=""

# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

log()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# 健康探测：只认 HTTP 2xx（curl -f 会把 4xx/5xx 当失败）。
# search-service 未就绪时 /actuator/health/readiness 返回 503，因此 -f 足够，不用抓 body。
probe() {
  curl -fsS -m 10 -o /dev/null "http://127.0.0.1:$1$2" 2>/dev/null
}

port_is_listening() {
  [ -n "$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true)" ]
}

# 某进程的工作目录。用于判断一个占着端口的进程到底是不是本项目起的。
pid_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

# 监听 <port> **且** 工作目录位于本项目之下的进程 pid。
# 两个条件必须同时成立，缺一不可：
#   1) 确实在监听这个端口；
#   2) cwd 在 $PROJECT_DIR 之内。
# 只匹配端口是不够的——别人的 Next.js 也可能占着 3000；只匹配命令名更糟——
# `node`/`java` 满机器都是。cwd 落在本仓库目录内这一条把范围收敛到「从本仓库启动的进程」，
# 而 Docker 的端口转发进程 cwd 不在这里，因此永远不会被命中。
owned_pids_on_port() {
  local port="$1" pid cwd
  for pid in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true); do
    cwd="$(pid_cwd "$pid")"
    case "$cwd" in
      "$PROJECT_DIR"|"$PROJECT_DIR"/*) printf '%s\n' "$pid" ;;
    esac
  done
}

pid_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

read_pidfile() {
  local f="$PID_DIR/$1.pid"
  [ -f "$f" ] || return 1
  tr -dc '0-9' < "$f"
}

# --------------------------------------------------------------------------
# 启动前的环境准备
# --------------------------------------------------------------------------

load_env() {
  [ -f "$PROJECT_DIR/.env" ] || die ".env 不存在。先跑 make bootstrap，或从 .env.example 复制一份。"
  [ -f "$PROJECT_DIR/dev.env" ] || die "dev.env 不存在（本地开发的连接串覆盖层）。"
  # 与 Makefile 的 DEV_ENV 完全一致：先 .env（含密钥），再叠加 dev.env（把 Docker
  # 主机名换成 localhost）。set -a 让赋值自动导出，五个子进程才读得到。
  set -a
  # shellcheck disable=SC1091
  . "$PROJECT_DIR/.env"
  # shellcheck disable=SC1091
  . "$PROJECT_DIR/dev.env"
  set +a
}

# 仓库统一 JDK 21，而 macOS 系统默认 java 常常不是 21。
# 这里只解析不报错：如果 search-service 本来就在跑，这一趟根本不需要 JDK。
resolve_java21() {
  if [ -x /usr/libexec/java_home ]; then
    JAVA21_HOME="$(/usr/libexec/java_home -v 21 2>/dev/null || true)"
  fi
  if [ -z "$JAVA21_HOME" ] && [ -n "${JAVA_HOME:-}" ]; then
    case "$("$JAVA_HOME/bin/java" -version 2>&1 | head -1)" in
      *'"21'*) JAVA21_HOME="$JAVA_HOME" ;;
    esac
  fi
}

# commerce 连的库和其它服务不是同一个。
#
# docker-compose.yml 给 commerce **单独**注入 postgresql://…/commerce；而 dev.env 是一层
# 扁平的全局覆盖，DATABASE_URL 只能有一个值，现在指向 searchops 库。于是本机进程形态下
# Medusa 会去 searchops 库里找自己的表，报
#     relation "tax_provider" does not exist
# 然后**挂住不退出**：模块加载器失败了，但 medusa develop 进程还活着，端口 9000 从头到尾
# 没人监听。症状因此不是"启动失败"而是"永远起不来"，光看进程在不在会得出相反结论。
#
# 这里按 compose 里的取值为 commerce 单独覆盖，是让本脚本与容器形态对齐的最小动作。
# 注意这是绕过不是修复：根因是 dev.env 无法表达"每个服务各自的连接串"，
# make dev-commerce 有一模一样的毛病。长久的修法在 dev.env / Makefile，不在本脚本。
# 想自己指定时设 COMMERCE_DATABASE_URL，本函数会原样采用。
commerce_database_url() {
  if [ -n "${COMMERCE_DATABASE_URL:-}" ]; then
    printf '%s' "$COMMERCE_DATABASE_URL"
    return 0
  fi
  # 只替换 URL 里的库名那一段，用户名/口令/主机/端口和可能的查询串都保持原样。
  printf '%s' "${DATABASE_URL:-}" | sed -E 's#(://[^/]+)/[^/?]+#\1/commerce#'
}

# 每个服务在本机怎么跑。与对应的 make dev-* 目标保持一致，避免两处漂移。
#
# 本函数总是在 $(...) 里被调用，而命令替换跑在子 shell 里：在这里调 die 只会结束子 shell，
# 父进程会拿着一条空命令继续往下跑。所以出错一律 stderr + return 1，由调用方 `|| die`。
service_command() {
  local commerce_url
  case "$1" in
    ai-adapter)
      # 注意 --reload：它的父进程即使子进程 import 失败也仍然持有监听 socket，
      # 表现是「连得上但没人应答」。下面的探测用的是带超时的 HTTP 请求而不是
      # 端口连通性，正是为了让这种情况暴露成超时失败而不是假成功。
      printf '%s' "cd '$PROJECT_DIR/services/ai-adapter' && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload"
      ;;
    search-service)
      if [ -z "$JAVA21_HOME" ]; then
        printf '找不到 JDK 21（/usr/libexec/java_home -v 21 无结果），无法启动 search-service。\n' >&2
        return 1
      fi
      printf '%s' "cd '$PROJECT_DIR/services/search-service' && export JAVA_HOME='$JAVA21_HOME' && export PATH=\"\$JAVA_HOME/bin:\$PATH\" && exec mvn spring-boot:run"
      ;;
    commerce)
      commerce_url="$(commerce_database_url)"
      if [ -n "$commerce_url" ]; then
        printf '%s' "cd '$PROJECT_DIR/services/commerce' && export DATABASE_URL=$(printf '%q' "$commerce_url") && exec npm run dev"
      else
        printf '%s' "cd '$PROJECT_DIR/services/commerce' && exec npm run dev"
      fi
      ;;
    storefront)        printf '%s' "cd '$PROJECT_DIR/apps/storefront' && exec npm run dev" ;;
    operations-console) printf '%s' "cd '$PROJECT_DIR/apps/operations-console' && exec npm run dev" ;;
    *) printf '未知服务：%s\n' "$1" >&2; return 1 ;;
  esac
}

# 失败时给出与本仓库真实踩过的坑对应的排查提示，而不是通用套话。
failure_hint() {
  case "$1" in
    ai-adapter)
      log "    提示：整片 TIMEOUT 时先看日志里有没有 import traceback。uvicorn --reload 的"
      log "          父进程在子进程 import 失败时仍持有监听 socket，症状是连得上但不应答。"
      ;;
    search-service)
      log "    提示：确认 JAVA_HOME 指向 JDK 21（当前解析为 ${JAVA21_HOME:-未解析}），"
      log "          以及 postgres/elasticsearch 容器是否 healthy。"
      ;;
    commerce)
      log "    提示：日志里若是 relation \"tax_provider\" does not exist，说明 Medusa 连错了库——"
      log "          它要的是 commerce 库而不是 searchops 库。此时进程还活着但 9000 永远没人监听，"
      log "          所以症状是超时而不是进程退出。当前用的库：$(commerce_database_url)"
      ;;
    storefront|operations-console)
      log "    提示：Next.js 的 /api/health 由探测请求本身触发编译；日志里若无编译输出，多半是端口或依赖问题。"
      ;;
  esac
}

# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

start_infra() {
  step "基础设施容器（$INFRA）"
  # docker compose up -d 本身幂等：已经 running 的容器不会被重建。
  docker compose up -d $INFRA
  local expected deadline healthy
  expected="$(printf '%s\n' $INFRA | wc -l | tr -d ' ')"
  deadline=$((SECONDS + INFRA_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    # 必须整行精确匹配：'healthy' 是 'unhealthy' 的子串，宽松匹配会把不健康的容器算成健康。
    healthy="$(docker compose ps --format '{{.Health}}' $INFRA 2>/dev/null | grep -c '^healthy$' || true)"
    if [ "$healthy" = "$expected" ]; then
      log "基础设施就绪：postgres:5432 · redis:6379 · elasticsearch:9200"
      return 0
    fi
    sleep 2
  done
  docker compose ps $INFRA || true
  die "基础设施在 ${INFRA_TIMEOUT}s 内没有全部 healthy。用 docker compose logs $INFRA 查看。"
}

# --------------------------------------------------------------------------
# 应用进程
# --------------------------------------------------------------------------

STATUS=""   # start_app 的返回状态，写全局是因为 bash 函数只能返回状态码
APP_PID=""

start_app() {
  local name="$1" port="$2" path="$3" timeout="$4"
  local pidfile="$PID_DIR/$name.pid" log_file="$LOG_DIR/$name.log"
  local pid="" cmd

  STATUS=""; APP_PID=""

  # 失效的 pid 文件（进程已经不在了）先清掉，避免它干扰后面的判断和 demo-down。
  pid="$(read_pidfile "$name" 2>/dev/null || true)"
  if ! pid_alive "$pid"; then
    rm -f "$pidfile"
    pid=""
  fi

  # 幂等判定分三种情况，顺序不能换：
  # 1) pid 文件里的进程还活着 → 这是上一次 demo-up 起的，不重复启动，只重新确认健康。
  if pid_alive "$pid"; then
    STATUS="already-running"; APP_PID="$pid"
  # 2) 端口上已经有健康的服务，但不是本脚本托管的（比如 make up 起的容器，或手工
  #    开的 make dev-*）→ 直接复用，不抢端口。demo-down 不会去动不是自己起的进程。
  elif probe "$port" "$path"; then
    STATUS="reused"; APP_PID=""
    printf '%-19s 端口 %s 上已有健康服务，跳过启动\n' "$name" "$port"
    return 0
  # 3) 端口被占但探测不健康 → 明确报冲突，不静默继续，也不去杀别人的进程。
  elif port_is_listening "$port"; then
    log "端口 $port 已被占用，但 http://127.0.0.1:$port$path 不健康。占用者："
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed 's/^/    /' || true
    die "$name 无法启动：端口 $port 冲突。若是 make up 起的容器，先 make down；否则先处理占用进程。"
  else
    STATUS="started"
    cmd="$(service_command "$name")" || die "$name 无法启动（见上一行）。"
    mkdir -p "$LOG_DIR" "$PID_DIR"
    printf '\n=== demo-up %s ===\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >> "$log_file"
    # set -m 打开作业控制，使每个后台任务获得**独立的进程组**（pgid == pid）。
    # 这是关键：mvn 会 fork java、npm 会 fork next/medusa、uvicorn --reload 会 fork
    # worker，只记父 pid 停不干净。有了独立进程组，demo-down 一发 kill -TERM -<pgid>
    # 就能精确停掉整棵子树，且不会波及进程组之外的任何东西。
    set -m
    nohup bash -c "$cmd" >> "$log_file" 2>&1 < /dev/null &
    pid=$!
    set +m
    APP_PID="$pid"
    printf '%s\n' "$pid" > "$pidfile"
  fi

  # 等就绪。每一轮都先确认进程还活着——进程已退出就不必再耗满超时预算。
  local deadline=$((SECONDS + timeout)) waited=0
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -n "$APP_PID" ] && ! pid_alive "$APP_PID"; then
      log ""
      log "$name 启动失败：进程 $APP_PID 已退出。"
      log "    日志：$log_file"
      tail -n 20 "$log_file" | sed 's/^/    | /'
      failure_hint "$name"
      die "$name 未能启动。其余已启动的服务仍在运行，收干净用 make demo-down。"
    fi
    if probe "$port" "$path"; then
      printf '%-19s ok  (%ss, pid %s)\n' "$name" "$waited" "${APP_PID:-external}"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
    if [ $((waited % 30)) -eq 0 ]; then
      printf '%-19s 等待就绪… %ss\n' "$name" "$waited"
    fi
  done

  log ""
  log "$name 在 ${timeout}s 内没有就绪（端口 $port，探测 $path）。"
  log "    日志：$log_file"
  tail -n 20 "$log_file" | sed 's/^/    | /'
  failure_hint "$name"
  die "$name 未能就绪。其余已启动的服务仍在运行，收干净用 make demo-down。"
}

# --------------------------------------------------------------------------
# 汇总表
# --------------------------------------------------------------------------

print_summary() {
  local i name port status pid
  printf '\n'
  printf '%-20s %-26s %-16s %-8s %s\n' SERVICE ADDRESS STATUS PID ""
  printf '%-20s %-26s %-16s %-8s %s\n' -------- ------- ------ --- ""
  printf '%-20s %-26s %-16s %-8s %s\n' postgres      "127.0.0.1:5432"        healthy - "容器"
  printf '%-20s %-26s %-16s %-8s %s\n' redis         "127.0.0.1:6379"        healthy - "容器"
  printf '%-20s %-26s %-16s %-8s %s\n' elasticsearch "http://127.0.0.1:9200" healthy - "容器"
  i=0
  while [ "$i" -lt "${#SERVICES[@]}" ]; do
    name="${SERVICES[$i]}"; port="${PORTS[$i]}"
    eval "status=\${STATUS_$i}"
    eval "pid=\${PID_$i}"
    printf '%-20s %-26s %-16s %-8s %s\n' \
      "$name" "http://127.0.0.1:$port" "$status" "${pid:--}" "本机进程 · ${LABELS[$i]}"
    i=$((i + 1))
  done
  printf '\n'
  log "日志：$LOG_DIR/<service>.log      pid：$PID_DIR/<service>.pid"
  log "入口：商城前台 http://127.0.0.1:3000 · 运营后台 http://127.0.0.1:3001"
  log "收干净：make demo-down（只停进程与容器，不动卷）"
  printf '\n'
}

# --------------------------------------------------------------------------

main() {
  mkdir -p "$PID_DIR" "$LOG_DIR"
  load_env
  resolve_java21
  start_infra

  step "应用进程（本机）"
  # 这层覆盖必须是看得见的，不能埋在脚本里让人以为 dev.env 就是对的。
  if [ -n "${DATABASE_URL:-}" ] && [ "$(commerce_database_url)" != "$DATABASE_URL" ]; then
    log "注：commerce 的 DATABASE_URL 单独指向 commerce 库（dev.env 的全局值指向 searchops 库，见脚本注释）。"
  fi
  local i
  i=0
  while [ "$i" -lt "${#SERVICES[@]}" ]; do
    start_app "${SERVICES[$i]}" "${PORTS[$i]}" "${HEALTH[$i]}" "${TIMEOUTS[$i]}"
    # bash 3.2 没有关联数组，用带下标的变量名保存每个服务的结果供汇总表使用。
    eval "STATUS_$i=\$STATUS"
    eval "PID_$i=\$APP_PID"
    i=$((i + 1))
  done

  print_summary
}

main "$@"
