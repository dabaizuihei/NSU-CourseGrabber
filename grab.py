#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
选课系统抢课脚本（通用可配置版）
=================================
针对学校选课系统 /xsxk 的体育/公共选修课抢课脚本。

【设计思路】
  1. 所有需要修改的参数都在 config.toml 中（TOML 格式，支持 # 注释），
     本文件无需改动
  2. 登录令牌（token）由用户在浏览器手动登录后获取，脚本不涉及密码/验证码
  3. 选课接口的 secretVal 参数每次查询都会变化，因此脚本在抢课过程中
     会定期重新查询课程列表，确保提交时使用最新的 secretVal

【执行流程】
  1. 读取并解析 config.toml，校验必填项
  2. 验证 token 有效性，并用服务器时间校准本地时钟偏移
     （避免本机时间不准导致开抢时机偏晚）
  3. 等待到 grab_time - pre_start_seconds 时刻，提前进入高频试探
  4. 循环：实时查询课程列表获取最新 secretVal → 提交选课 → 处理结果
  5. 提交返回 200（仅代表进入选课队列）后，轮询「已选课程」列表二次确认，
     确认选上才停止；入队未确认不判成功，而是继续复查/重新提交，
     直到确认成功、连续多次未确认、达到 max_attempts 或 end_time

【错误提示约定】
  脚本对常见失败原因（token 失效、网络异常、课程未找到、时间未到等）
  都会在日志中给出可能原因和解决建议，方便排查。
"""

import json
import os
import sys
import time
import datetime
import logging
import argparse
try:
    import tomllib  # Python 3.11+ 标准库
except ModuleNotFoundError:
    import tomli as tomllib  # Python 3.10 及以下需 pip install tomli
import urllib.request
import urllib.parse
import urllib.error

# ----------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.toml")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# 模拟浏览器 UA，部分服务器会拦截无 UA 或脚本特征的请求
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 等待阶段重新校准时钟偏移的间隔（秒），防止长时间挂机后本地时钟漂移
TIME_RECALIBRATE_INTERVAL = 300


# ----------------------------------------------------------------
# 配置解析（TOML 格式）
# ----------------------------------------------------------------
def load_config(path):
    """
    读取并解析 TOML 格式配置文件（config.toml）。

    TOML 是 Python 3.11+ 标准库原生支持的配置格式：
      - 注释用 # 开头
      - 字符串用双引号包裹，数字直接书写（解析后自动为 int/float）
      - 用 [section] 分段组织配置
    解析失败时打印具体行号和常见原因后退出。
    """
    if not os.path.exists(path):
        print("[配置错误] 找不到配置文件: {}".format(os.path.basename(path)))
        print("          请确认脚本目录下存在 {}".format(os.path.basename(path)))
        sys.exit(1)
    try:
        # tomllib 要求以二进制模式打开
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print("[配置错误] {} 格式有误，无法解析。".format(os.path.basename(path)))
        print("          错误位置: 第 {} 行".format(e.lineno))
        print("          常见原因: 字符串漏了双引号、[section] 不匹配、")
        print("                    键值后多了逗号等")
        print("          提示: 可对照 config.toml 中的注释示例检查")
        sys.exit(1)

    # 将 [grab] 段的键提升到顶层，保持脚本内部读取方式不变
    # （config.toml 中用 [grab] 分组更清晰，脚本读取时无需关心分组）
    grab_cfg = cfg.pop("grab", {})
    cfg.update(grab_cfg)
    return cfg


# ----------------------------------------------------------------
# 日志
# ----------------------------------------------------------------
def setup_logger():
    """初始化日志：同时输出到控制台和 logs/ 目录下的时间戳文件。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(
        LOG_DIR, "grab_{}.log".format(
            datetime.datetime.now().strftime("%Y%m%d_%H%M%S")))
    logger = logging.getLogger("grab")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    # 控制台输出
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    # 文件输出（UTF-8，中文不乱码）
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger, log_file


def parse_time(text, logger):
    """
    将 'YYYY-MM-DD HH:MM:SS' 字符串解析为 datetime 对象。
    格式错误时打印提示并退出（避免脚本带着错误时间空等）。
    """
    try:
        return datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.error("时间格式错误: %s（应为 YYYY-MM-DD HH:MM:SS，如 2026-09-14 13:00:00）",
                     text)
        sys.exit(1)


# ----------------------------------------------------------------
# 核心抢课类
# ----------------------------------------------------------------
class Grabber:
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self.base = cfg["base_url"].rstrip("/")
        self.token = cfg["token"]
        self.batch_id = cfg["batch_id"]
        self.timeout = float(cfg.get("request_timeout", 15))
        # 服务器时间与本地时间的偏移量（毫秒），由 calibrate_time() 校准
        self.offset_ms = 0
        # 是否已打印过 token 失效的详细提示（避免重复刷屏）
        self._token_warned = False
        # 公共请求头：token 放在 Authorization，轮次 ID 放在 batchId
        self.headers = {
            "Authorization": self.token,
            "batchId": self.batch_id,
            "User-Agent": UA,
        }

    # ---------------------- 网络请求层 ----------------------

    def post(self, path, data, as_json=False):
        """
        统一的 POST 请求封装。

        参数:
          path    - 接口路径，如 /elective/clazz/list
          data    - 请求体（字典）
          as_json - True 发送 JSON 格式；False 发送表单格式（与前端行为一致）

        返回:
          接口返回的 JSON 字典；请求失败时返回 {"code": -1, ...} 并在
          msg 中带上原因，绝不抛出异常中断主流程。
        """
        if as_json:
            body = json.dumps(data).encode("utf-8")
            content_type = "application/json"
        else:
            body = urllib.parse.urlencode(data).encode("utf-8")
            content_type = "application/x-www-form-urlencoded"

        req = urllib.request.Request(
            self.base + path, data=body,
            headers={**self.headers, "Content-Type": content_type})

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 服务器返回了非 2xx 状态码（如 401/403/500 等）
            body = e.read().decode("utf-8", "ignore")[:200]
            return {"code": e.code, "msg": "HTTP {}".format(e.code),
                    "_raw": body}
        except urllib.error.URLError as e:
            # 网络层错误：DNS 失败、连接被拒、超时等
            return {"code": -2, "msg": "网络错误: {}".format(e.reason)}
        except (json.JSONDecodeError, ValueError) as e:
            # 服务器返回了非 JSON 内容（可能是 302 跳转到登录页 / 网关错误页）
            return {"code": -3, "msg": "返回内容不是JSON(可能已跳转登录页)",
                    "detail": str(e)}
        except Exception as e:
            # 兜底捕获，避免脚本因未知异常退出
            return {"code": -1, "msg": "未知异常: {}".format(e)}

    # ---------------------- 时间校准 ----------------------

    def server_now_ms(self):
        """获取服务器当前时间（毫秒时间戳）；失败返回 None。"""
        r = self.post("/web/now", {})
        if r.get("code") == 200 and r.get("data", {}).get("currentTime"):
            return int(r["data"]["currentTime"])
        return None

    def calibrate_time(self):
        """
        校准本地与服务器的时间偏移，同时验证 token 是否有效。

        说明: /web/now 通常无需登录即可访问，因此这里能成功只能证明网络通；
              真正的 token 有效性会在后续选课接口调用中体现（302/401）。
        """
        local_ms = int(time.time() * 1000)
        server_ms = self.server_now_ms()
        if server_ms is None:
            self.log.error("无法获取服务器时间。")
            self.log.error("可能原因: ① 网络不通或系统维护 ② 本机无法访问该域名")
            self.log.error("处理建议: 先浏览器打开选课系统确认可访问，再重试")
            sys.exit(1)
        self.offset_ms = server_ms - local_ms
        self.log.info("服务器时间: %s", self._fmt_ms(server_ms))
        self.log.info("本地时间  : %s", self._fmt_ms(local_ms))
        self.log.info("时间偏移  : %+.1f 秒（已自动校准，开抢以服务器时间为准）",
                      self.offset_ms / 1000.0)
        return server_ms

    def server_now(self):
        """返回校准后的服务器时间（datetime 对象）。"""
        return datetime.datetime.fromtimestamp(
            (time.time() * 1000 + self.offset_ms) / 1000.0)

    def try_recalibrate(self):
        """
        等待期间定期重新校准时钟偏移（长时间挂机时本地时钟可能漂移）。
        与 calibrate_time 不同：失败只警告、不退出，避免等待阶段因
        一次网络波动就终止整个抢课任务。
        """
        server_ms = self.server_now_ms()
        if server_ms is None:
            self.log.warning("重新校准时间失败（网络波动？），继续沿用上次偏移量。")
            return False
        local_ms = int(time.time() * 1000)
        self.offset_ms = server_ms - local_ms
        self.log.info("等待期间重新校准: 时间偏移 %+.1f 秒",
                      self.offset_ms / 1000.0)
        return True

    @staticmethod
    def _fmt_ms(ms):
        return datetime.datetime.fromtimestamp(ms / 1000.0).strftime(
            "%Y-%m-%d %H:%M:%S.%f")[:-3]

    # ---------------------- 选课业务接口 ----------------------

    def fetch_target(self):
        """
        查询课程列表，定位目标教学班（按 course.KCH 课程号 + course.KXH 课序号匹配）。

        返回:
          {"JXBID": 教学班ID, "secretVal": 选课密钥, "name": 课程名}
          未找到返回 None。
        注意: secretVal 每次查询都会变化，提交前必须重新获取，
              这也是脚本在抢课循环中定期刷新它的原因。
        """
        data = {
            "teachingClassType": self.cfg["teaching_class_type"],
            "pageNumber": 1,
            "pageSize": 200,
            "campus": self.cfg.get("campus", "1"),
            "KCH": self.cfg["course"]["KCH"],
        }
        r = self.post("/elective/clazz/list", data, as_json=True)

        # 非 200：给出原因提示
        if r.get("code") != 200:
            self._log_list_error(r)
            return None

        kxh = str(self.cfg["course"]["KXH"])
        rows = r.get("data", {}).get("rows", [])
        for course in rows:
            # 体育项目类返回结构：课程下有 tcList（多个教学班）
            for tc in course.get("tcList", []) or []:
                if str(tc.get("KXH")) == kxh:
                    # 课程名来自服务器：课程名 + 项目名 + 任课教师
                    name = "{} {}".format(course.get("KCM", ""),
                                          tc.get("sportName", ""))
                    teacher = tc.get("SKJS", "")
                    if teacher:
                        name = "{}-{}".format(name, teacher)
                    return {
                        "JXBID": tc.get("JXBID"),
                        "secretVal": tc.get("secretVal"),
                        "name": name,
                    }
            # 通用返回结构：课程本身就是一个教学班
            if str(course.get("KXH")) == kxh:
                return {
                    "JXBID": course.get("JXBID"),
                    "secretVal": course.get("secretVal"),
                    "name": course.get("KCM", ""),
                }
        # 列表正常返回但没匹配到目标课程
        self.log.warning("未在课程列表中匹配到 KCH=%s KXH=%s 的教学班",
                         self.cfg["course"]["KCH"], kxh)
        self.log.warning("可能原因: ① 课程号/课序号填错 ② 该课程不在当前轮次或校区 ③ 选课尚未开放，课程还未挂出")
        self.log.warning("处理建议: 核对 config.toml 的 course.KCH / course.KXH，并对照选课页课程列表确认")
        return None

    def _log_list_error(self, r):
        """根据列表接口返回码输出针对性的原因提示（token 失效只详细提示一次）。"""
        code = r.get("code")
        if code in (-3, -2, -1):
            # 返回内容非 JSON / 网络错误：大概率是 token 失效被 302 到登录页
            if code == -3:
                if not self._token_warned:
                    self.log.warning("课程列表查询失败: %s", r.get("msg"))
                    self.log.warning("可能原因: 登录令牌(token)已失效，服务器将请求跳转到了登录页")
                    self.log.warning("处理建议: 重新登录选课系统，按 F12 控制台执行 "
                                     "sessionStorage.getItem('token') 获取新 token，"
                                     "更新 config.toml 后重试")
                    self._token_warned = True
                else:
                    self.log.warning("课程列表查询失败: %s（判断为 token 失效，"
                                     "连续失败将自动退出）", r.get("msg"))
            else:
                self.log.warning("课程列表查询失败: %s（网络异常，稍后自动重试）",
                                 r.get("msg"))
        else:
            self.log.warning("课程列表查询失败: code=%s msg=%s",
                             code, r.get("msg", ""))

    def submit(self, clazz_id, secret_val, is_confirm=False):
        """
        提交选课请求。

        参数:
          clazz_id   - 教学班 ID（JXBID）
          secret_val - 选课密钥（必须是最新查询得到的值）
          is_confirm - 是否携带 isConfirm=1（处理 code=301 需确认的场景）

        返回: 服务器 JSON 响应。
        """
        payload = {
            "clazzType": self.cfg["teaching_class_type"],
            "clazzId": clazz_id,
            "secretVal": secret_val,
        }
        if is_confirm:
            payload["isConfirm"] = "1"
        return self.post("/elective/clazz/add", payload)

    def already_selected(self):
        """查询已选课程，判断目标课程是否已选上（避免重复提交/误判）。"""
        r = self.post("/elective/select", {})
        if r.get("code") != 200:
            # 查询失败不阻塞主流程，返回 False 继续抢课
            return False
        kxh = str(self.cfg["course"]["KXH"])
        for item in r.get("data", []) or []:
            if str(item.get("KXH")) == kxh and \
               str(item.get("KCH")) == str(self.cfg["course"]["KCH"]):
                return True
        return False

    def confirm_selected(self, timeout=8):
        """
        提交返回 200 后，轮询 /elective/select 确认目标课是否真的进入已选列表。
        金智的 /add 只是把请求送入选课队列，真正选上需服务端处理；
        这里等待几秒做二次确认，避免把「进入队列」误判为「选上」。
        无论 timeout 多小都至少完整查询一次，避免配置为 0 时直接误判。
        """
        deadline = time.time() + timeout
        while True:
            if self.already_selected():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    # ---------------------- 主流程 ----------------------

    def run(self):
        cfg = self.cfg
        # 解析关键时间/频率配置（格式错误会在这里直接退出并提示）
        grab_dt = parse_time(cfg["grab_time"], self.log)
        end_dt = parse_time(cfg.get("end_time", "2099-01-01 00:00:00"), self.log)
        pre_start = float(cfg.get("pre_start_seconds", 5))
        interval = float(cfg.get("retry_interval", 0.5))
        max_attempts = int(cfg.get("max_attempts", 1200))
        refresh_every = int(cfg.get("secret_refresh_every", 3))
        confirm_timeout = float(cfg.get("confirm_timeout", 8))
        max_unconfirmed = int(cfg.get("max_unconfirmed", 3))

        # 打印本次任务概要
        self.log.info("=" * 60)
        self.log.info("抢课目标: KCH=%s KXH=%s",
                      cfg["course"]["KCH"], cfg["course"]["KXH"])
        self.log.info("抢课时间: %s（提前 %s 秒开始试探）",
                      grab_dt.strftime("%Y-%m-%d %H:%M:%S"), pre_start)
        self.log.info("重试间隔: %s 秒 | 最大尝试: %s 次",
                      interval, max_attempts)
        self.log.info("入队确认: 提交后等待 %s 秒复查已选列表，连续 %s 次未确认则退出",
                      confirm_timeout, max_unconfirmed)
        self.log.info("=" * 60)

        # 1. 网络检查 + 服务器时间校准
        self.calibrate_time()

        # 2. 等待到开抢前 pre_start 秒（此阶段只打印倒计时，不发请求）
        last_print = -1
        last_cal = time.time()   # 上次时间校准时刻（用于长时间等待期间重校准）
        while True:
            now_server = self.server_now()
            remain = (grab_dt - now_server).total_seconds()
            if remain <= pre_start:
                break
            if now_server >= end_dt:
                self.log.warning("已超过截止时间 %s，脚本退出。",
                                 end_dt.strftime("%Y-%m-%d %H:%M:%S"))
                sys.exit(0)
            # 长时间挂机等待时本地时钟可能漂移，每 5 分钟重新校准一次；
            # 仅在距开抢还有 60 秒以上时执行，避免影响最后的开抢时机
            if remain > 60 and time.time() - last_cal >= TIME_RECALIBRATE_INTERVAL:
                self.try_recalibrate()
                last_cal = time.time()
            # 控制倒计时打印频率：60 秒以上每 30 秒打一次，60 秒内每 10 秒一次
            if remain >= 60:
                print_interval = 30
            else:
                print_interval = 1
            bucket = int(remain) // print_interval
            if bucket != last_print:
                self.log.info("距离开抢还有 %s 秒（按服务器时间）",
                              max(0, int(remain)))
                last_print = bucket
            time.sleep(1)

        self.log.info(">>> 进入抢课阶段！时间: %s",
                      self.server_now().strftime("%Y-%m-%d %H:%M:%S"))

        # 3. 抢课循环：查最新 secretVal -> 提交 -> 按结果处理
        attempt = 0
        last_refresh = 0
        target_cache = None
        list_fail_streak = 0   # 连续获取课程列表失败的次数（用于判断 token 失效）
        unconfirmed = 0        # 连续「入队但未确认选上」的次数
        while attempt < max_attempts:
            attempt += 1
            now_server = self.server_now()
            # 到达放弃时间仍未成功 -> 退出
            if now_server >= end_dt:
                self.log.warning("已到截止时间 %s，仍未成功，脚本退出。",
                                 end_dt.strftime("%Y-%m-%d %H:%M:%S"))
                self.log.warning("若已开抢仍一直失败，请查看上方日志中的原因提示")
                sys.exit(2)

            # 第 1 次尝试前先查已选列表，防止重复提交；
            # 此前若有「入队但未确认」的提交，每轮也先复查（队列可能已处理完成）
            if attempt == 1 or unconfirmed > 0:
                if self.already_selected():
                    if attempt == 1:
                        self.log.info("目标课程已在已选列表中，无需抢课，脚本退出。")
                    else:
                        self.log.info("✅ 此前入队的请求已在已选课程列表中确认到，抢课成功！")
                    sys.exit(0)

            # 定期刷新课程信息（secretVal 会变，必须用最新的）
            if target_cache is None or (attempt - last_refresh) >= refresh_every:
                target = self.fetch_target()
                if target and target.get("secretVal"):
                    target_cache = target
                    last_refresh = attempt
                    list_fail_streak = 0   # 获取成功，重置连续失败计数
                    self.log.info("已获取教学班 %s (%s)",
                                  target_cache["JXBID"],
                                  target_cache.get("name", ""))
                else:
                    list_fail_streak += 1
                    # 清掉缓存，避免用过期 secretVal 提交（secretVal 每次查询都会变）
                    target_cache = None
                    self.log.warning("第 %s 次尝试：未能获取目标教学班信息，稍后重试", attempt)
                    # 连续多次获取失败：大概率 token 已失效，尽早退出而不是空耗
                    if list_fail_streak >= 5:
                        self.log.error("连续 %s 次无法获取课程列表，判断为登录令牌失效或系统异常，抢课无法继续。",
                                       list_fail_streak)
                        self.log.error("处理建议: 重新登录选课系统，按 F12 控制台执行 "
                                       "sessionStorage.getItem('token') 获取新 token，"
                                       "更新 config.toml 后重新运行")
                        sys.exit(4)

            if target_cache is None:
                time.sleep(interval)
                continue

            # 提交选课
            r = self.submit(target_cache["JXBID"], target_cache["secretVal"])
            code = r.get("code")
            msg = r.get("msg", "")
            tag = "第 {:>4} 次".format(attempt)

            # code=301：服务器要求二次确认（如超容量/跨年级等），自动带 isConfirm
            # 重提交，重提交结果与首次提交共用下方 code==200 的确认逻辑
            if code == 301:
                self.log.info("%s ⚠️ 服务器要求确认（%s），自动带 isConfirm 重提交…",
                              tag, msg)
                r2 = self.submit(target_cache["JXBID"],
                                 target_cache["secretVal"],
                                 is_confirm=True)
                code, msg = r2.get("code"), r2.get("msg", "")

            # 提交返回 200 仅代表请求进入选课队列，真正选上以「已选课程」
            # 列表为准。因此入队后必须二次确认；确认不到不判成功、不退出，
            # 而是继续复查已选列表并重新提交，避免把「进入队列」误判为「选上」。
            if code == 200:
                self.log.info("%s ✅ 提交返回 200，已进入选课队列！课程: %s",
                              tag, target_cache.get("name", ""))
                self.log.info("教学班: %s", target_cache["JXBID"])
                if self.confirm_selected(timeout=confirm_timeout):
                    self.log.info("✅ 已在已选课程列表中确认，抢课成功！")
                    sys.exit(0)
                # 入队但未确认：可能是队列仍在处理，也可能名额已满排队失败
                unconfirmed += 1
                self.log.warning("%s ⚠️ 入队后 %s 秒未在已选列表确认到（第 %s/%s 次），"
                                 "继续复查重试…", tag, confirm_timeout,
                                 unconfirmed, max_unconfirmed)
                if unconfirmed >= max_unconfirmed:
                    # 退出前再做一次较长的复查：最后一次入队仍在服务端队列
                    # 处理中，给它更长时间确认，避免把最终会成功的入队误判失败
                    self.log.warning("连续 %s 次入队未即时确认，做最后一次复查（最长 %s 秒）…",
                                     unconfirmed, confirm_timeout * 4)
                    if self.confirm_selected(timeout=confirm_timeout * 4):
                        self.log.info("✅ 最终复查确认已选上，抢课成功！")
                        sys.exit(0)
                    self.log.error("连续 %s 次入队均未能确认选上，可能名额已满或队列处理异常。",
                                   unconfirmed)
                    self.log.error("说明: 金智 /add 只入队，最终结果以教务系统「已选课程」为准。")
                    self.log.error("处理建议: 稍后登录选课系统人工核对；若未选上，"
                                   "可更新 token 重新运行本脚本。")
                    sys.exit(5)
                target_cache = None   # 下轮强制重新获取最新 secretVal 再提交
                time.sleep(interval)
                continue

            # 登录态失效：明确提示并退出
            if code in (401, 402, 403):
                self.log.error("%s ❌ 登录状态失效（code=%s），抢课无法继续。",
                               tag, code)
                self.log.error("可能原因: token 已过期或已在其他设备重新登录")
                self.log.error("处理建议: 重新登录选课系统，按 F12 控制台执行 "
                               "sessionStorage.getItem('token') 获取新 token，更新 config.toml 后重新运行")
                sys.exit(3)

            # 选课尚未开始：服务器会返回「本轮次选课暂未开始」，属正常现象
            if code == 500 and ("未开始" in msg or "暂未" in msg):
                self.log.info("%s 选课尚未开始（服务器返回: %s），继续试探…",
                              tag, msg or "")

            # 其他失败：打印原因，稍后重试
            else:
                self.log.info("%s 提交未成功: code=%s msg=%s",
                              tag, code, msg or "")
                if code == -3:
                    self.log.warning("提示: 返回内容异常，多半是 token 失效，请留意后续日志")

            time.sleep(interval)

        # 重试次数耗尽
        self.log.error("已达最大尝试次数 %s，抢课未成功，脚本退出。", max_attempts)
        self.log.error("提示: 请检查上方日志中的原因提示，必要时更新 token / 课程配置后重跑")
        sys.exit(2)


# ----------------------------------------------------------------
# 程序入口
# ----------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="选课系统抢课脚本")
    parser.add_argument("-c", "--config", default=CONFIG_PATH,
                        help="配置文件路径（默认: config.toml）")
    parser.add_argument("-t", "--time", dest="grab_time", default=None,
                        help="临时覆盖抢课时间，格式 YYYY-MM-DD HH:MM:SS（用于测试）")
    args = parser.parse_args()

    # 读取配置（TOML 格式，格式错误会提示后退出）
    cfg = load_config(args.config)

    # 命令行参数可临时覆盖抢课时间（如测试用）
    if args.grab_time:
        cfg["grab_time"] = args.grab_time

    # 必填项校验
    required = ["base_url", "token", "batch_id", "teaching_class_type",
                "grab_time"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print("[配置错误] config.toml 缺少必填项: {}".format(", ".join(missing)))
        print("          请参照 README.md 和 config.toml 中的注释补齐")
        sys.exit(1)
    if not cfg.get("course", {}).get("KCH") or not cfg["course"].get("KXH"):
        print("[配置错误] config.toml 中 [course] 段的 KCH / KXH 不能为空")
        sys.exit(1)

    # 初始化日志并启动
    logger, log_file = setup_logger()
    logger.info("日志文件: %s", log_file)
    Grabber(cfg, logger).run()


if __name__ == "__main__":
    main()
