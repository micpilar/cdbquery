#!/usr/bin/env python3
import sys
import time
import argparse
import requests
import threading
import concurrent.futures
import chess
import chess.engine

# --- Logging Setup ---
class LogLevel:
    ERROR = 1
    WARNING = 2
    INFO = 3
    DEBUG = 4
    TRACE = 5

class Logger:
    COLORS = {
        LogLevel.ERROR: "\033[91m",   # Red
        LogLevel.WARNING: "\033[93m", # Yellow
        LogLevel.INFO: "\033[94m",    # Blue
        LogLevel.DEBUG: "\033[92m",   # Green
        LogLevel.TRACE: "\033[90m"    # Grey
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def __init__(self, level=LogLevel.INFO):
        self.level = level

    def _log(self, level_num, level_name, msg):
        if self.level >= level_num:
            timestamp = time.strftime("%H:%M:%S")
            color = self.COLORS[level_num]
            msg = msg.replace("**", self.BOLD)
            msg = msg.replace(self.RESET, self.RESET + color) 
            print(f"{color}[{timestamp}] [{level_name}] {msg}{self.RESET}", flush=True)

    def error(self, msg):   self._log(LogLevel.ERROR, "ERROR", msg)
    def warning(self, msg): self._log(LogLevel.WARNING, "WARN ", msg)
    def info(self, msg):    self._log(LogLevel.INFO, "INFO ", msg)
    def debug(self, msg):   self._log(LogLevel.DEBUG, "DEBUG", msg)
    def trace(self, msg):   self._log(LogLevel.TRACE, "TRACE", msg)

# Global logger instance
logger = Logger()

# --- Concurrency Management ---

class MultiSemaphore:
    """A custom semaphore that allows atomic acquisition of multiple tokens to prevent deadlocks."""
    def __init__(self, max_tokens):
        self.tokens = max_tokens
        self.cond = threading.Condition()
        
    def acquire(self, n):
        with self.cond:
            while self.tokens < n:
                self.cond.wait()
            self.tokens -= n
            
    def release(self, n):
        with self.cond:
            self.tokens += n
            self.cond.notify_all()

# --- Network & API Functions ---

def cdb_get(base_url, params=None, stream=False):
    req = requests.models.PreparedRequest()
    req.prepare_url(base_url, params)
    url_str = req.url
    
    while True:
        try:
            logger.trace(f"Querying: {url_str}")
            resp = requests.get(base_url, params=params, stream=stream, timeout=30)
            
            if resp.status_code == 429:
                time.sleep(0.5)
                continue

            if not stream:
                text = resp.text.replace('\x00', '').strip()
                logger.trace(f"Response: {text}")
                if "rate limit exceeded" in text.lower():
                    time.sleep(0.5)
                    continue
                return text
            else:
                if 'text/event-stream' not in resp.headers.get('Content-Type', ''):
                    text = resp.text.replace('\x00', '').strip()
                    logger.trace(f"Stream Init Response: {text}")
                    if "rate limit exceeded" in text.lower():
                        time.sleep(0.5)
                        continue
                logger.trace("Stream connection successfully established.")
                return resp

        except requests.RequestException as e:
            logger.debug(f"Network error: {e}, retrying in 1s...")
            time.sleep(1)

def query_pv(fen):
    full_pv = []
    current_fen = fen
    
    while True:
        res = cdb_get("http://www.chessdb.cn/cdb.php", params={"action": "querypv", "board": current_fen, "stable": "1"})
        chunk = []
        if "pv:" in res:
            pv_str = res.split("pv:")[1].strip()
            if pv_str:
                chunk = [m.strip() for m in pv_str.split("|") if m.strip()]
        
        if not chunk:
            break
            
        full_pv.extend(chunk)
        
        if len(chunk) >= 200:
            board = chess.Board(current_fen)
            valid = True
            for raw_move in chunk:
                move_str = raw_move.split(",")[0].rstrip("+#?!").strip()
                if ":" in move_str: move_str = move_str.split(":")[-1]
                try:
                    m = chess.Move.from_uci(move_str)
                    if m in board.legal_moves:
                        board.push(m)
                    else:
                        valid = False
                        break
                except ValueError:
                    valid = False
                    break
            if not valid or board.is_game_over():
                break
            current_fen = board.fen()
        else:
            break
            
    return full_pv

def run_local_stockfish(fen, args_dict, multi_sem):
    """Executes a local Stockfish instance via python-chess engine."""
    threads = args_dict['threads']
    multi_sem.acquire(threads)
    
    try:
        engine = chess.engine.SimpleEngine.popen_uci(args_dict['path'])
        
        # Configure Engine
        options = {"Threads": threads, "Hash": args_dict['hash']}
        if args_dict['syzygy']:
            options["SyzygyPath"] = args_dict['syzygy']
        engine.configure(options)
        
        board = chess.Board(fen)
        l_type, l_val = args_dict['limit']
        
        # Build Limit
        if l_type == 'depth':
            limit = chess.engine.Limit(depth=int(l_val))
        elif l_type == 'nodes':
            limit = chess.engine.Limit(nodes=int(l_val))
        elif l_type == 'time':
            limit = chess.engine.Limit(time=float(l_val))
        elif l_type == 'clock':
            limit = chess.engine.Limit(white_clock=float(l_val), black_clock=float(l_val))
        else:
            limit = chess.engine.Limit(white_clock=2.0, black_clock=2.0)
            
        logger.debug(f"[Local SF] Analyzing {fen.split()[0]} with limit {l_type} {l_val}...")
        
        info = engine.analyse(board, limit)
        engine.quit()
        
        depth = info.get("depth", 0)
        nodes = info.get("nodes", 0)
        nps = info.get("nps", 0)
        
        score_obj = info.get("score")
        if score_obj:
            score_val = score_obj.relative
            if score_val.is_mate():
                score_str = f"mate {score_val.mate()}"
            else:
                score_str = f"cp {score_val.score()}"
        else:
            score_str = "cp 0"
            
        pv_moves = info.get("pv", [])
        pv_str = " ".join(m.uci() for m in pv_moves)
        
        info_line = f"info depth {depth} score {score_str} nodes {nodes} nps {nps} pv {pv_str}"
        logger.info(f"[Local SF] **{info_line}**")
        
        return [m.uci() for m in pv_moves]
        
    except Exception as e:
        logger.error(f"[Local SF] Engine failed: {e}")
        return []
    finally:
        multi_sem.release(threads)

def _single_sf_query(fen, target_len, stop_event):
    """Executes a single, raw Stockfish SSE request. Can be forcefully killed via stop_event."""
    try:
        req = requests.get("https://chessdb.cn/stockfish.php", params={"board": fen}, stream=True, timeout=15)
        pvs_by_depth = {}
        max_depth_seen = -1
        stream_valid = False
        
        for line in req.iter_lines():
            if stop_event.is_set():
                req.close()
                return []
                
            if not line:
                continue
            
            decoded = line.decode('utf-8', errors='ignore').replace('\x00', '').strip()
            
            if "rate limit exceeded" in decoded.lower():
                req.close()
                return []
            
            if decoded.startswith("data: "):
                content = decoded[6:].strip()
                
                if content == "[DONE]":
                    stream_valid = True
                    break 
                    
                if content.startswith("info"):
                    parts = content.split()
                    current_depth = -1
                    if "depth" in parts:
                        try:
                            idx = parts.index("depth")
                            current_depth = int(parts[idx+1])
                        except (ValueError, IndexError):
                            pass
                            
                    if " pv " in content:
                        pv_list = content.split(" pv ")[1].strip().split()
                        if current_depth != -1:
                            pvs_by_depth[current_depth] = pv_list
                            if current_depth > max_depth_seen:
                                max_depth_seen = current_depth
                        else:
                            max_depth_seen += 1
                            pvs_by_depth[max_depth_seen] = pv_list
                            
        req.close()

        if not pvs_by_depth or not stream_valid:
            return []
            
        last_pv = pvs_by_depth.get(max_depth_seen, [])
        if len(last_pv) >= target_len:
            return last_pv
            
        valid_candidates = {d: p for d, p in pvs_by_depth.items() if len(p) >= target_len}
        if valid_candidates:
            best_depth = max(valid_candidates.keys())
            return valid_candidates[best_depth]
        else:
            longest_len = max(len(p) for p in pvs_by_depth.values())
            longest_candidates = {d: p for d, p in pvs_by_depth.items() if len(p) == longest_len}
            best_depth = max(longest_candidates.keys())
            return longest_candidates[best_depth]

    except Exception:
        return []

def get_stockfish_pv(fen, target_len, local_args, multi_sem):
    """Spawns 4 concurrent requests for the same FEN. Retries up to 5 times. Falls back to local."""
    for attempt in range(1, 6):
        stop_event = threading.Event()
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_single_sf_query, fen, target_len, stop_event) for _ in range(4)]
            
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    stop_event.set()
                    return res
                    
        logger.trace(f"Online streams dropped for {fen.split()[0]}. Attempt {attempt}/5...")
        time.sleep(1)
        
    logger.warning(f"Online Stockfish failed 5 times for {fen.split()[0]}. Falling back to Local Engine...")
    return run_local_stockfish(fen, local_args, multi_sem)

def queue_worker(fen):
    res = cdb_get("http://www.chessdb.cn/cdb.php", params={"action": "queue", "board": fen})
    if res == "ok":
        logger.debug(f"Successfully queued FEN: **{fen}**")
    else:
        logger.warning(f"Queue response: {res} for FEN: {fen}")

def check_score_worker(fen):
    res = cdb_get("http://www.chessdb.cn/cdb.php", params={"action": "queryscore", "board": fen})
    return fen, res

def poll_positions(fens, fens_cache, cycle_iteration):
    resolved = set()
    total = len(fens)
    
    while len(resolved) < total:
        pending_fens = [f for f in fens if f not in resolved]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(pending_fens))) as executor:
            results = executor.map(check_score_worker, pending_fens)
            
        for fen, res in results:
            if res.startswith("eval:") or res in ("checkmate", "stalemate"):
                logger.info(f"Scored position [{fen}] -> **{res}**")
                resolved.add(fen)
                fens_cache[fen] = cycle_iteration
            elif res == "invalid board":
                logger.error(f"Invalid board (Dropping from queue) [{fen}]")
                resolved.add(fen)
            else:
                logger.debug(f"Pending evaluation [{fen}] -> {res}")
        
        if len(resolved) < total:
            logger.info(f"**{len(resolved)}/{total}** positions scored. Sleeping 2s...")
            time.sleep(2)

def generate_ply_fens(base_fen, depth, filter_mode, filter_moves):
    if depth == 0:
        return [base_fen]
        
    current_positions = [(chess.Board(base_fen), 0)]
    
    for d in range(1, depth + 1):
        next_positions = []
        for board, _ in current_positions:
            for move in board.legal_moves:
                if d == 1 and filter_mode:
                    san = board.san(move)
                    uci = move.uci()
                    matches = (san in filter_moves) or (uci in filter_moves)
                    if filter_mode == 'include' and not matches:
                        continue
                    if filter_mode == 'exclude' and matches:
                        continue
                
                next_board = board.copy()
                next_board.push(move)
                next_positions.append((next_board, d))
        current_positions = next_positions
        
    return list(dict.fromkeys([b.fen() for b, _ in current_positions]))

# --- Main Flow ---

def main():
    parser = argparse.ArgumentParser(description="ChessDB Auto-Queuer via Stockfish API")
    
    # Core Args
    parser.add_argument("--fen", type=str, required=True, help="Initial FEN string")
    parser.add_argument("--depth", type=int, required=True, help="Number of positions to extract/queue")
    parser.add_argument("--ply", nargs="+", help="Generate root positions X plies deep. Syntax: <depth> [exclude|include] [move1 move2...]")
    parser.add_argument("--concurrency", type=int, default=3, help="Run SF concurrently on the last X moves of the PV (default: 3). Ignored if --no-engine.")
    parser.add_argument("--cache", type=int, default=5, help="Avoid re-queueing/evaluating FENs touched in the last X iterations (default: 5)")
    parser.add_argument("--tb", action="store_true", help="Allow processing and queueing of Tablebase positions (7 pieces or fewer).")
    parser.add_argument("--no-poll", action="store_true", help="Fire-and-forget: Queue positions but do not wait for evaluations.")
    parser.add_argument("--no-engine", action="store_true", help="Bypass Stockfish completely. Directly queue positions from the end of the known PV.")
    parser.add_argument("--log-level", type=str, choices=["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"], default="INFO", help="Set the logging level (default: INFO)")
    
    # Local Stockfish Fallback Args
    parser.add_argument("--sf-path", type=str, default="stockfish", help="Path to local Stockfish binary (default: 'stockfish').")
    parser.add_argument("--sf-threads", type=int, default=1, help="Threads for each local Stockfish process (default: 1).")
    parser.add_argument("--sf-hash", type=int, default=16, help="Hash size in MB for each local Stockfish (default: 16).")
    parser.add_argument("--sf-limit", nargs=2, default=["clock", "2.0"], help="Limit for local SF. Syntax: <type> <val> (e.g., 'clock 2.0', 'depth 20') (default: clock 2.0).")
    parser.add_argument("--sf-syzygy", type=str, default=None, help="Path to Syzygy tablebases for local Stockfish.")
    parser.add_argument("--sf-max-threads", type=int, default=4, help="Max total global threads allowed to be occupied by local Stockfish instances concurrently (default: 4).")
    
    args = parser.parse_args()

    level_map = {"ERROR": 1, "WARNING": 2, "INFO": 3, "DEBUG": 4, "TRACE": 5}
    logger.level = level_map[args.log_level]

    try:
        root_board = chess.Board(args.fen)
    except ValueError:
        logger.error("Invalid starting FEN provided.")
        sys.exit(1)

    if not args.tb and len(root_board.piece_map()) <= 7:
        logger.error("Initial FEN is a TB7 position but --tb was not passed. Exiting.")
        sys.exit(1)
        
    if args.sf_threads > args.sf_max_threads:
        logger.warning(f"Local threads per process ({args.sf_threads}) exceeds max global threads ({args.sf_max_threads}). Clamping to {args.sf_max_threads}.")
        args.sf_threads = args.sf_max_threads

    local_sf_args = {
        'path': args.sf_path,
        'threads': args.sf_threads,
        'hash': args.sf_hash,
        'syzygy': args.sf_syzygy,
        'limit': args.sf_limit
    }
    local_sf_semaphore = MultiSemaphore(args.sf_max_threads)

    ply_offset = 0
    root_fens = [args.fen]
    
    if args.ply:
        try:
            ply_offset = int(args.ply[0])
        except ValueError:
            logger.error("First argument to --ply must be an integer depth.")
            sys.exit(1)
            
        filter_mode = None
        filter_moves = []
        if len(args.ply) >= 2:
            filter_mode = args.ply[1].lower()
            if filter_mode not in ['include', 'exclude']:
                logger.error("Filter mode must be 'include' or 'exclude'.")
                sys.exit(1)
            filter_moves = args.ply[2:]
            
        logger.info(f"Generating positions **{ply_offset}** plies deep from root...")
        if filter_mode:
            logger.info(f"Applying filter on Ply 1: **{filter_mode.upper()} [{', '.join(filter_moves)}]**")
            
        raw_roots = generate_ply_fens(args.fen, ply_offset, filter_mode, filter_moves)
        
        if not args.tb:
            root_fens = [f for f in raw_roots if len(chess.Board(f).piece_map()) > 7]
            if len(root_fens) < len(raw_roots):
                logger.info(f"Filtered out **{len(raw_roots) - len(root_fens)}** TB7 root positions.")
        else:
            root_fens = raw_roots

        if not root_fens:
            logger.error("Ply generation resulted in 0 valid non-TB7 positions. Check your filters.")
            sys.exit(1)
            
        logger.info(f"Successfully generated **{len(root_fens)}** target roots for concurrent analysis.")

    sf_cache = {}    
    fens_cache = {}  
    iteration = 1

    # Determines how many targets we pull from the end of the PV backwards
    target_limit = args.depth if args.no_engine else args.concurrency

    while True:
        iter_start_time = time.time()
        print(f"\n{Logger.BOLD}{Logger.COLORS[LogLevel.INFO]}=== ITERATION {iteration} ==={Logger.RESET}")
        
        logger.info(f"Fetching PVs for **{len(root_fens)}** root positions from ChessDB concurrently...")
        root_pvs = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(64, len(root_fens))) as executor:
            future_to_root = {executor.submit(query_pv, r): r for r in root_fens}
            for future in concurrent.futures.as_completed(future_to_root):
                root = future_to_root[future]
                try:
                    root_pvs[root] = future.result()
                except Exception as e:
                    logger.error(f"Failed to fetch PV for root {root}: {e}")
                    root_pvs[root] = []

        all_targets = [] 
        
        for idx, root_fen in enumerate(root_fens):
            cdb_pv = root_pvs[root_fen]
            board = chess.Board(root_fen)
            pv_fens = [board.fen()]
            
            for raw_move in cdb_pv:
                move_str = raw_move.split(",")[0].rstrip("+#?!").strip()
                if ":" in move_str: move_str = move_str.split(":")[-1]
                try:
                    m = chess.Move.from_uci(move_str)
                    if m in board.legal_moves:
                        board.push(m)
                        pv_fens.append(board.fen())
                        if board.is_game_over() or (not args.tb and len(board.piece_map()) <= 7):
                            break
                    else: break
                except ValueError: break

            target_info = []
            for depth, f in reversed(list(enumerate(pv_fens))):
                b = chess.Board(f)
                if b.is_game_over() or (not args.tb and len(b.piece_map()) <= 7): 
                    continue
                
                age = iteration - sf_cache.get(f, -999)
                if age <= args.cache:
                    continue
                
                target_info.append((depth, f))
                if len(target_info) == target_limit:
                    break

            if not target_info:
                if len(root_fens) > 1:
                    logger.warning(f"[Root **{idx+1}**] All PV nodes fully cached (Deadlock detected). **Deleting cache for this root and resuming normally...**")
                    for f in pv_fens:
                        sf_cache.pop(f, None)
                        fens_cache.pop(f, None)
                else:
                    logger.warning(f"[Root **{idx+1}**] All PV nodes fully cached (Deadlock detected). **Deleting global cache and resuming normally...**")
                    sf_cache.clear()
                    fens_cache.clear()

                for depth, f in reversed(list(enumerate(pv_fens))):
                    b = chess.Board(f)
                    if not b.is_game_over() and (args.tb or len(b.piece_map()) > 7):
                        target_info.append((depth, f))
                        if len(target_info) == target_limit:
                            break
                            
            for depth, t_fen in target_info:
                all_targets.append((idx + 1, root_fen, depth, t_fen))

        if not all_targets:
            logger.info("No non-game-over targets exist across any lines. Waiting 5s...")
            time.sleep(5)
            continue

        all_fens_to_poll = []
        seen = set()

        if args.no_engine:
            logger.info(f"No-Engine Mode: Queueing **{len(all_targets)}** aggregated PV positions directly...")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as queue_executor:
                for root_idx, _, depth, t_fen in all_targets:
                    sf_cache[t_fen] = iteration # Mark target as processed for backtracking
                    
                    abs_depth = depth + ply_offset
                    
                    if args.no_poll:
                        if t_fen not in seen:
                            seen.add(t_fen)
                            logger.info(f"[Root **{root_idx}**] Queueing target from Depth **{abs_depth}**...")
                            queue_executor.submit(queue_worker, t_fen)
                            all_fens_to_poll.append(t_fen)
                    else:
                        age = iteration - fens_cache.get(t_fen, -999)
                        if age > args.cache and t_fen not in seen:
                            seen.add(t_fen)
                            logger.info(f"[Root **{root_idx}**] Queueing target from Depth **{abs_depth}**...")
                            queue_executor.submit(queue_worker, t_fen)
                            all_fens_to_poll.append(t_fen)
                        else:
                            logger.debug(f"[Root {root_idx}] [Depth {abs_depth}] Skipping cached evaluated target FEN: {t_fen}")
                            
        else:
            logger.info(f"Unleashing Online Stockfish shotgun (4 streams per target) on **{len(all_targets)}** aggregated positions...")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(all_targets))) as sf_executor, \
                 concurrent.futures.ThreadPoolExecutor(max_workers=32) as queue_executor:
                
                future_to_target = {
                    sf_executor.submit(get_stockfish_pv, t_fen, args.depth, local_sf_args, local_sf_semaphore): (root_idx, depth, t_fen) 
                    for root_idx, _, depth, t_fen in all_targets
                }
                
                for future in concurrent.futures.as_completed(future_to_target):
                    root_idx, depth, target_fen = future_to_target[future]
                    sf_cache[target_fen] = iteration 
                    
                    try:
                        sf_pv = future.result()
                    except Exception as e:
                        logger.warning(f"[Root **{root_idx}**] Stockfish query failed at depth {depth}: {e}")
                        continue
                    
                    if not sf_pv:
                        logger.warning(f"[Root **{root_idx}**] [Depth **{depth}**] Both Online and Local Stockfish failed to return a PV. Skipping.")
                        continue
                        
                    limit = min(args.depth, len(sf_pv))
                    abs_depth = depth + ply_offset 
                    
                    logger.info(f"[Root **{root_idx}**] [Depth **{abs_depth}**] PV for [{target_fen}]: **{' '.join(sf_pv[:limit])}**...")
                    
                    sf_board = chess.Board(target_fen)
                    candidates = []
                    
                    for move_str in sf_pv[:limit]:
                        try:
                            m = chess.Move.from_uci(move_str)
                            if m in sf_board.legal_moves:
                                sf_board.push(m)
                                candidate = sf_board.fen()
                                
                                if not args.tb and len(sf_board.piece_map()) <= 7:
                                    logger.debug(f"[Root {root_idx}] [Depth {abs_depth}] Hit TB7 candidate, stopping PV extraction early.")
                                    break
                                
                                if args.no_poll:
                                    if candidate not in seen:
                                        candidates.append(candidate)
                                        seen.add(candidate)
                                else:
                                    age = iteration - fens_cache.get(candidate, -999)
                                    if age > args.cache and candidate not in seen:
                                        candidates.append(candidate)
                                        seen.add(candidate)
                                    else:
                                        pass
                            else: break
                        except ValueError: break
    
                    if candidates:
                        logger.info(f"[Root **{root_idx}**] Queueing **{len(candidates)}** fresh candidates from Depth **{abs_depth}**...")
                        for c in candidates:
                            queue_executor.submit(queue_worker, c)
                            all_fens_to_poll.append(c)
                    else:
                        logger.warning(f"[Root **{root_idx}**] [Depth **{abs_depth}**] All candidates were TB7, cached, or queued by another thread. Skipping.")

        # Post-Execution Polling
        if not all_fens_to_poll:
            logger.info("No new positions were queued in this iteration.")
        elif not args.no_poll:
            logger.info(f"All positions queued successfully. Waiting for **{len(all_fens_to_poll)}** evaluations...")
            poll_positions(all_fens_to_poll, fens_cache, iteration)
        else:
            logger.info(f"Fire-and-forget: **{len(all_fens_to_poll)}** positions queued globally, skipping evaluation polling.")

        elapsed = round(time.time() - iter_start_time, 2)
        logger.info(f"Iteration {iteration} completed across {len(root_fens)} roots in **{elapsed}s**.")
        
        iteration += 1

if __name__ == "__main__":
    main()
