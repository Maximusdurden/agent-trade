import sqlite3
import json
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("Trading Agent DB Server")

# The path to your SQLite database
DB_PATH = r"Z:\python\projects\agent-trade\trading_agent.db"

def get_connection():
    """Create a read-only database connection."""
    # Using URI with mode=ro ensures AI agents cannot accidentally modify or drop tables
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

@mcp.resource("sqlite://schema")
def get_schema() -> str:
    """Get the database schema to understand available tables and columns."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            schema = [row[0] for row in cursor.fetchall() if row[0]]
            return "\n\n".join(schema)
    except Exception as e:
        return f"Error fetching schema: {e}"

@mcp.tool()
def query_trading_db(sql_query: str) -> str:
    """
    Execute a read-only SQL SELECT query against the trading_agent database.
    
    Args:
        sql_query: The SQL SELECT query to execute.
    """
    # Basic safety check to prevent accidental modifications
    if not sql_query.strip().upper().startswith(("SELECT", "WITH", "EXPLAIN")):
        return "Error: Only SELECT queries are allowed."
        
    try:
        with get_connection() as conn:
            # Return rows as dictionaries for clean JSON serialization
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql_query)
            
            # Fetch results (limiting to 100 rows to prevent massive token usage)
            rows = cursor.fetchmany(100)
            
            if not rows:
                return "Query executed successfully, but returned no results."
                
            result = [dict(row) for row in rows]
            return json.dumps(result, indent=2)
            
    except sqlite3.Error as e:
        return f"Database error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    # Run the server using standard input/output (required for MCP)
    mcp.run()
