import sys
import chess
import chess.pgn

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 chess_converter.py startpos moves m1 m2 m3 ...")
        print("  python3 chess_converter.py fen <FEN> moves m1 m2 m3 ...")
        print("  python3 chess_converter.py <FEN> moves m1 m2 m3 ...")
        sys.exit(1)

    # 1. Join all arguments into a single string
    # This allows users to run it without quoting the FEN string in the terminal
    raw_input = " ".join(sys.argv[1:]).strip()

    # Clean up standard UCI engine prefix if present
    if raw_input.startswith("position "):
        raw_input = raw_input[9:]

    # 2. Separate the position part from the moves part
    if " moves " in raw_input:
        pos_part, moves_part = raw_input.split(" moves ", 1)
        moves_list = moves_part.split()
    elif raw_input.endswith(" moves"):
        pos_part = raw_input[:-6]
        moves_list = []
    else:
        # No 'moves' keyword provided; assume the whole string is just the position
        pos_part = raw_input
        moves_list = []

    pos_part = pos_part.strip()

    # 3. Determine the FEN based on the position string
    if pos_part == "startpos":
        fen = chess.STARTING_FEN
    elif pos_part.startswith("fen "):
        fen = pos_part[4:].strip()
    else:
        # If they just provided the FEN directly without "fen " keyword
        fen = pos_part

    # 4. Initialize the board
    try:
        board = chess.Board(fen)
    except ValueError as e:
        print(f"Error parsing FEN: {e}")
        sys.exit(1)

    # 5. Initialize the PGN Game
    game = chess.pgn.Game()
    game.setup(board) # Sets [FEN] and [SetUp "1"] headers (omitted automatically if startpos)
    
    # 6. Apply the moves
    node = game
    for move_str in moves_list:
        try:
            # Try parsing as UCI (e.g., "e2e4")
            move = board.parse_uci(move_str)
        except ValueError:
            try:
                # Fallback to SAN (e.g., "e4", "Nf3")
                move = board.parse_san(move_str)
            except ValueError:
                print(f"Error: Invalid or illegal move '{move_str}' for the current position.")
                sys.exit(1)

        # Add move to the PGN tree
        node = node.add_variation(move)
        
        # Push the move to the board (handles 50-move rule, en passant, castling rights, etc.)
        board.push(move)

    # 7. Output the results
    print("=== PGN ===")
    print(game, "\n")
    
    print("=== Final FEN ===")
    print(board.fen())

if __name__ == "__main__":
    main()
