import argparse
import sys
import os
import re
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

try:
    import chess
except ImportError:
    print("Error: The 'python-chess' library is required for the piece counting feature.", file=sys.stderr)
    print("Install it using: pip install python-chess", file=sys.stderr)
    sys.exit(1)

# Regex to remove ANSI color codes from terminal output
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def parse_openings(filepath):
    """Parses a TSV file with format: opening_name \t fen"""
    openings = {}
    if not filepath:
        return openings
    with open(filepath, 'r') as f:
        for line in f:
            line = ansi_escape.sub('', line).strip()
            if not line or '\t' not in line:
                continue
            name, fen = line.split('\t', 1)
            openings[name.strip()] = fen.strip()
    return openings

def parse_duration(duration_str):
    match = re.match(r"^(\d+)([smhd])$", duration_str.strip().lower())
    if not match:
        raise ValueError(f"Invalid duration format: '{duration_str}'.")
    val, unit = int(match.group(1)), match.group(2)
    units = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days'}
    return timedelta(**{units[unit]: val})

def iter_log_file(filepath, do_invert, pass_num):
    """Yields parsed log components strictly line-by-line to avoid string accumulation."""
    with open(filepath, 'r') as f:
        for line in f:
            line = ansi_escape.sub('', line).strip()
            if not line: continue
            try:
                left_part, right_part = line.split('cp --')
                time_str, cp_str = left_part.rsplit(':', 1)
                t = datetime.fromisoformat(time_str.strip())
                cp = int(cp_str.strip())
                if do_invert: cp = -cp
                moves = right_part.strip().split()
                yield t, cp, len(moves), moves
            except Exception as e:
                # Only warn during the first pass to avoid console spam
                if pass_num == 1:
                    print(f"Warning: Could not parse line in {filepath}:\n{line}\nError: {e}", file=sys.stderr)
                continue

class Bin:
    """Tracks absolute visual extremes per resolution segment to prevent loss of visual spikes."""
    __slots__ = ['first', 'last', 'max_cp', 'min_cp', 'max_depth', 'min_pc']
    
    def __init__(self):
        self.first = None
        self.last = None
        self.max_cp = None
        self.min_cp = None
        self.max_depth = None
        self.min_pc = None

    def add(self, pt):
        # pt is (time, cp, depth, pc)
        if self.first is None:
            self.first = self.last = self.max_cp = self.min_cp = self.max_depth = self.min_pc = pt
            return
        
        self.last = pt
        if pt[1] > self.max_cp[1]: self.max_cp = pt
        if pt[1] < self.min_cp[1]: self.min_cp = pt
        if pt[2] > self.max_depth[2]: self.max_depth = pt
        if pt[3] is not None and (self.min_pc[3] is None or pt[3] < self.min_pc[3]):
            self.min_pc = pt

    def get_sorted_points(self):
        pts = {self.first, self.last, self.max_cp, self.min_cp, self.max_depth, self.min_pc}
        pts.discard(None)
        # Sequence points chronologically to prevent step plot backwards lines
        return sorted(list(pts), key=lambda x: x[0])

def main():
    parser = argparse.ArgumentParser(description="Plot chess evaluation (cp), depth, and pieces over time using O(1) memory.")
    parser.add_argument('-f', '--files', nargs='+', required=True, help="Input log files")
    parser.add_argument('-l', '--labels', nargs='+', help="Labels for files")
    parser.add_argument('-i', '--invert', nargs='+', help="1/0 to invert scores")
    parser.add_argument('-o', '--output', default='eval_plot.png', help="Output filename")
    parser.add_argument('--openings', type=str, help="TSV file: opening_name \t fen")
    parser.add_argument('--start', type=str, help='Start time ISO')
    parser.add_argument('--end', type=str, help='End time ISO')
    parser.add_argument('--last', type=str, help='Last X time (e.g. 10m)')

    args = parser.parse_args()

    labels = args.labels if args.labels else [os.path.basename(f) for f in args.files]
    if len(labels) != len(args.files):
        sys.exit("Error: --labels count must match --files count.")

    invert_flags = [False] * len(args.files)
    if args.invert:
        invert_flags = [val.lower() in ('1', 'true', 't', 'y') for val in args.invert]

    openings_dict = parse_openings(args.openings)
    
    file_stats = []
    global_max_time = datetime.min
    has_piece_counts = False

    # --- PASS 1: Identify Extents & Console Output (O(1) memory) ---
    for filepath, label, do_invert in zip(args.files, labels, invert_flags):
        base_name = os.path.basename(filepath)
        opening_key = base_name[:-4] if base_name.endswith('.log') else base_name
        has_pc = opening_key in openings_dict
        if has_pc:
            has_piece_counts = True
            
        stats = {
            'label': f"{label} (Inverted)" if do_invert else label,
            'filepath': filepath,
            'do_invert': do_invert,
            'opening_key': opening_key,
            'has_pc': has_pc,
            'min_cp': float('inf'),
            'max_cp': float('-inf'),
            'max_depth': -1,
            'max_depth_pvs': set(),
            'min_pc': float('inf'),
            'min_pc_data': set(),
            'min_time': datetime.max,
            'max_time': datetime.min,
        }

        for t, cp, depth, moves in iter_log_file(filepath, do_invert, pass_num=1):
            if t < stats['min_time']: stats['min_time'] = t
            if t > stats['max_time']: stats['max_time'] = t
            
            if cp < stats['min_cp']: stats['min_cp'] = cp
            if cp > stats['max_cp']: stats['max_cp'] = cp
            
            if depth > stats['max_depth']:
                stats['max_depth'] = depth
                stats['max_depth_pvs'] = {" ".join(moves)}
            elif depth == stats['max_depth']:
                if len(stats['max_depth_pvs']) < 15: # Cap size dynamically
                    stats['max_depth_pvs'].add(" ".join(moves))
                    
            if has_pc:
                board = chess.Board(openings_dict[opening_key])
                for m in moves:
                    try: board.push(chess.Move.from_uci(m))
                    except: break
                pc = len(board.piece_map())
                
                pv_str = " ".join(moves)
                if pc < stats['min_pc']:
                    stats['min_pc'] = pc
                    stats['min_pc_data'] = {(board.fen(), pv_str)}
                elif pc == stats['min_pc']:
                    if len(stats['min_pc_data']) < 15:
                        stats['min_pc_data'].add((board.fen(), pv_str))
                        
        if stats['max_time'] != datetime.min:
            global_max_time = max(global_max_time, stats['max_time'])
            
        file_stats.append(stats)
        
        # Original script's console output format
        print(f"\n{'='*60}\nLOGFILE: {stats['label']} ({filepath})\n{'='*60}")
        if stats['min_time'] == datetime.max:
            print("  No valid data found.")
            continue
            
        print(f"  Score: Min={stats['min_cp']}, Max={stats['max_cp']}")
        print(f"  Depth: Max={stats['max_depth']}")
        print(f"  PV line(s) with biggest depth:")
        for mpv in sorted(stats['max_depth_pvs']):
            print(f"    >>> {mpv}")
            
        if has_pc:
            print(f"\n  Piece Count: Min={stats['min_pc']}")
            print(f"  States with minimum pieces:")
            for fen, pv in sorted(stats['min_pc_data']):
                print(f"    - FEN: {fen}\n      PV:  {pv}")

    # Viewport Bounds Setup
    T_start, T_end = None, None
    if args.end: T_end = datetime.fromisoformat(args.end)
    if args.start: T_start = datetime.fromisoformat(args.start)
    if args.last:
        ref = T_end if T_end else global_max_time
        if ref != datetime.min:
            T_start = ref - parse_duration(args.last)
            T_end = ref

    plot_start = T_start
    plot_end = T_end
    
    valid_mins = [s['min_time'] for s in file_stats if s['min_time'] != datetime.max]
    if not plot_start and valid_mins:
        plot_start = min(valid_mins)
    if not plot_end and global_max_time != datetime.min:
        plot_end = global_max_time
        
    time_span = (plot_end - plot_start).total_seconds() if (plot_start and plot_end) else 0

    # --- PASS 2: Downsampled Viewport Assembly (O(1) Memory) ---
    NUM_BINS = 4000 # Horizontal resolution of bins
    
    for stats in file_stats:
        if stats['min_time'] == datetime.max:
            continue
            
        bins = {}
        last_pt_before = None
        first_pt_after = None
        has_pc = stats['has_pc']
        
        for t, cp, depth, moves in iter_log_file(stats['filepath'], stats['do_invert'], pass_num=2):
            pc = None
            if has_pc:
                board = chess.Board(openings_dict[stats['opening_key']])
                for m in moves:
                    try: board.push(chess.Move.from_uci(m))
                    except: break
                pc = len(board.piece_map())
                
            pt = (t, cp, depth, pc)
            
            # Step-Plot edge continuity boundaries
            if T_start and t < T_start:
                last_pt_before = pt
                continue
            if T_end and t > T_end:
                if first_pt_after is None:
                    first_pt_after = pt
                continue
                
            # Disperse values linearly into resolution bins
            if time_span <= 0: b_idx = 0
            else: b_idx = int(((t - plot_start).total_seconds() / time_span) * NUM_BINS)
                
            if b_idx not in bins: bins[b_idx] = Bin()
            bins[b_idx].add(pt)
            
        times, cps, depths, pcs = [], [], [], []
        
        if last_pt_before:
            times.append(last_pt_before[0])
            cps.append(last_pt_before[1])
            depths.append(last_pt_before[2])
            if has_pc: pcs.append(last_pt_before[3])
            
        for b_idx in sorted(bins.keys()):
            for pt in bins[b_idx].get_sorted_points():
                times.append(pt[0])
                cps.append(pt[1])
                depths.append(pt[2])
                if has_pc: pcs.append(pt[3])
                
        if first_pt_after:
            times.append(first_pt_after[0])
            cps.append(first_pt_after[1])
            depths.append(first_pt_after[2])
            if has_pc: pcs.append(first_pt_after[3])
            
        stats['times'] = times
        stats['cps'] = cps
        stats['depths'] = depths
        if has_pc:
            stats['pcs'] = pcs

    # --- PLOTTING ---
    fig, ax1 = plt.subplots(figsize=(15, 8))
    if has_piece_counts: fig.subplots_adjust(right=0.82)
    
    ax2 = ax1.twinx()
    ax3 = ax1.twinx() if has_piece_counts else None
    if ax3: ax3.spines['right'].set_position(('outward', 60))

    cmap = plt.get_cmap('tab20')
    color_idx = 0
    score_lines, depth_lines, piece_dots = [], [], []

    for data in file_stats:
        if 'times' not in data or not data['times']: continue
        
        # Score - Solid Line
        c1 = cmap(color_idx % 20); color_idx += 1
        l1, = ax1.step(data['times'], data['cps'], where='post', color=c1, linewidth=2.5, label=data['label'])
        score_lines.append(l1)

        # Depth - Solid Line
        c2 = cmap(color_idx % 20); color_idx += 1
        l2, = ax2.step(data['times'], data['depths'], where='post', color=c2, linewidth=1.5, label=data['label'])
        depth_lines.append(l2)

        # Pieces - Dots Only
        if data['has_pc'] and ax3:
            c3 = cmap(color_idx % 20); color_idx += 1
            l3, = ax3.plot(data['times'], data['pcs'], color=c3, marker='o', 
                           linestyle='None', markersize=4, alpha=0.9, label=data['label'])
            piece_dots.append(l3)

    if T_start or T_end: ax1.set_xlim(left=T_start, right=T_end)

    # Score Axis Framing based on the viewport limits
    visible_cps = [cp for d in file_stats if 'times' in d for t, cp in zip(d['times'], d['cps']) 
                   if (not T_start or t >= T_start) and (not T_end or t <= T_end)]
    if visible_cps:
        mi, ma = min(visible_cps), max(visible_cps)
        pad = max((ma - mi) * 0.1, 20)
        ax1.set_ylim(mi - pad, ma + pad)
    ax1.axhline(0, color='black', linewidth=1, alpha=0.3)

    # Formatting
    ax1.set_xlabel('Time', fontweight='bold')
    ax1.set_ylabel('Score (CP)', fontweight='bold')
    ax2.set_ylabel('Depth (Moves)', fontweight='bold')
    if ax3: ax3.set_ylabel('Piece Count', fontweight='bold')

    locator = mdates.AutoDateLocator()
    ax1.xaxis.set_major_locator(locator)
    t_range = mdates.num2date(ax1.get_xlim()[1]) - mdates.num2date(ax1.get_xlim()[0])
    fmt = '%H:%M:%S' if t_range < timedelta(minutes=15) else '%Y-%m-%d %H:%M'
    ax1.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    fig.autofmt_xdate()

    ax1.legend(handles=score_lines, loc='upper left', title="Score (Solid Line)")
    ax2.legend(handles=depth_lines, loc='upper center', title="Depth (Solid Line)")
    if piece_dots:
        ax3.legend(handles=piece_dots, loc='upper right', title="Pieces (Dots)")

    plt.title('Chess Analysis: Score, Depth, and Material', fontsize=14, pad=20)
    ax1.grid(True, alpha=0.3)
    plt.savefig(args.output, dpi=150, bbox_inches='tight')
    print(f"\nSuccess! Plot saved to: {args.output}")

if __name__ == '__main__':
    main()
