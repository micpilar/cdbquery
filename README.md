# cdbquery
Short scripts for chessDBCN

1. cdbpv: gets the stable pv of a position and tries to extend it; possible variants:
   – with online stockfish (deprecated endpoint)
   – with local stockfish (specify path to executable, threads, hash, limit to search, path to syzygy, max threads for all stockfish processes)
   – with no engine (specify `--no-engine`)
   You can specify the starting FEN via `--fen`, number of positions to queue at one iteration (starts from the last move of the pv and goes backwards) via `--depth`, `--cache` (remembers what positions were queued in the last n iterations and skips them), `--tb` to enable queueing positions in tablebases (7-men or fewer), `--ply` to enable polling a few moves at once, syntax is `--ply {depth} {include/exclude} {move1, move2, move3, ...}` (if exclude is passed it takes all moves from perft {depth} except these)

2. fentopgn: takes a string in the format `FEN moves m1 m2 m3 ...` (like what cdb outputs with copy FEN) and converts it to a PGN

*Note: This is ai slop generated with gemini 3.1 pro*
