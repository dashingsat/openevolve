"""
Utilities for code parsing, diffing, and manipulation
"""

import re
from typing import Dict, List, Optional, Tuple, Union


def parse_evolve_blocks(code: str) -> List[Tuple[int, int, str]]:
    """
    Parse evolve blocks from code

    Args:
        code: Source code with evolve blocks

    Returns:
        List of tuples (start_line, end_line, block_content)
    """
    lines = code.split("\n")
    blocks = []

    in_block = False
    start_line = -1
    block_content = []

    for i, line in enumerate(lines):
        if "# EVOLVE-BLOCK-START" in line:
            in_block = True
            start_line = i
            block_content = []
        elif "# EVOLVE-BLOCK-END" in line and in_block:
            in_block = False
            blocks.append((start_line, i, "\n".join(block_content)))
        elif in_block:
            block_content.append(line)

    return blocks


def apply_diff(original_code: str, diff_text: str) -> str:
    """
    Apply a diff to the original code

    Args:
        original_code: Original source code
        diff_text: Diff in the SEARCH/REPLACE format

    Returns:
        Modified code
    """
    # Split into lines for easier processing
    original_lines = original_code.split("\n")
    result_lines = original_lines.copy()

    # Extract diff blocks
    diff_blocks = extract_diffs(diff_text)

    # Apply each diff block
    import logging
    logger = logging.getLogger(__name__)
    
    for i, (search_text, replace_text) in enumerate(diff_blocks):
        search_lines = search_text.split("\n")
        replace_lines = replace_text.split("\n")
        
        # Normalize whitespace for matching (ignoring leading/trailing whitespace on lines)
        # But we still need to apply replacement accurately
        
        found = False
        # Try exact match first
        for i in range(len(result_lines) - len(search_lines) + 1):
            if result_lines[i : i + len(search_lines)] == search_lines:
                result_lines[i : i + len(search_lines)] = replace_lines
                found = True
                break
        
        if found:
            continue
            
        # Try looser match (ignoring trailing whitespace)
        search_lines_stripped = [l.rstrip() for l in search_lines]
        
        # Also try ignoring leading/trailing blank lines in search block
        # (LLMs often add extra newlines)
        while search_lines_stripped and not search_lines_stripped[0]:
            search_lines_stripped.pop(0)
        while search_lines_stripped and not search_lines_stripped[-1]:
            search_lines_stripped.pop()
            
        if not search_lines_stripped:
            # Empty search block?
            continue

        for i in range(len(result_lines) - len(search_lines_stripped) + 1):
            # We need to match against a slice of result_lines that also ignores blank lines?
            # Or just match the non-blank content against the file?
            # Matching strictly against the file lines is safer.
            
            # Check if this slice matches
            slice_to_check = [l.rstrip() for l in result_lines[i : i + len(search_lines_stripped)]]
            if slice_to_check == search_lines_stripped:
                # Found it! Now we need to replace the CORRESPONDING lines in result_lines.
                # But wait, we stripped blank lines from search_lines.
                # We should replace the range [i : i + len(search_lines_stripped)].
                # But if the original search block had blank lines we stripped, 
                # we are replacing a smaller chunk than intended?
                # Actually, usually the intent is to replace the "code part". 
                # If the LLM added blank lines to search, it implies it thought they were there.
                # If we ignore them, we match the code.
                # Replacing just the code part is usually correct.
                
                result_lines[i : i + len(search_lines_stripped)] = replace_lines
                found = True
                break
                
        if not found:
            logger.warning(f"Failed to apply diff block {i+1}. Search text:\n{search_text}\nCould not find match in original code.")

    return "\n".join(result_lines)


def extract_diffs(diff_text: str) -> List[Tuple[str, str]]:
    """
    Extract diff blocks from the diff text

    Args:
        diff_text: Diff in the SEARCH/REPLACE format

    Returns:
        List of tuples (search_text, replace_text)
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # Debug: Log raw diff text if extraction fails (or always for now to debug)
    # logger.info(f"DEBUG: Extracting diffs from:\n{diff_text[:500]}...")

    diff_pattern = r"<<<<<<< SEARCH\n(.*?)=======\n(.*?)>>>>>>> REPLACE"
    diff_blocks = re.findall(diff_pattern, diff_text, re.DOTALL)
    
    if not diff_blocks:
       # Less strict pattern for edge cases
       diff_pattern = r"<<<<<<< SEARCH(.*?)\n?=======\n?(.*?)>>>>>>> REPLACE"
       diff_blocks = re.findall(diff_pattern, diff_text, re.DOTALL)
       
    if not diff_blocks:
        # Check for truncated response (missing closing tag)
        # If we have SEARCH and ======= but no REPLACE tag, we might be able to salvage it
        truncated_pattern = r"<<<<<<< SEARCH(.*?)\n?=======\n?(.*)$"
        truncated_match = re.search(truncated_pattern, diff_text, re.DOTALL)
        if truncated_match:
            search_content = truncated_match.group(1).rstrip()
            replace_content = truncated_match.group(2).rstrip()
            
            # Auto-heal heuristic:
            # If replacement ends unexpectedly, try to close the list if it looks like a list.
            if replace_content.strip().startswith("INDEX_CANDIDATES = ["):
                # Check if it's missing the closing brace/bracket
                if not replace_content.strip().endswith("]"):
                    logger.warning("Auto-healing truncated list in diff response.")
                    
                    # Smart salvage: Find the last successfully closed item
                    last_brace_index = replace_content.rfind("}")
                    
                    if last_brace_index != -1:
                        # Truncate everything after the last closed dictionary
                        # and close the list cleanly
                        replace_content = replace_content[:last_brace_index+1] + "]"
                        logger.warning("Recovered truncated diff block by trimming partial items.")
                    else:
                        # No valid items found? Revert to empty list to avoid syntax error
                        replace_content = "INDEX_CANDIDATES = []"
                        logger.warning("Recovered truncated diff block by resetting to empty list.")
            
            return [(search_content, replace_content)]
            
        logger.warning(f"Failed to extract diffs. Raw text:\n{diff_text}")
       
    return [(match[0].rstrip(), match[1].rstrip()) for match in diff_blocks]


def parse_full_rewrite(llm_response: str, language: str = "python") -> Optional[str]:
    """
    Extract a full rewrite from an LLM response

    Args:
        llm_response: Response from the LLM
        language: Programming language

    Returns:
        Extracted code or None if not found
    """
    code_block_pattern = r"```" + language + r"\n(.*?)```"
    matches = re.findall(code_block_pattern, llm_response, re.DOTALL)

    if matches:
        return matches[0].strip()

    # Fallback to any code block
    code_block_pattern = r"```(.*?)```"
    matches = re.findall(code_block_pattern, llm_response, re.DOTALL)

    if matches:
        return matches[0].strip()

    # Fallback to plain text
    return llm_response


def format_diff_summary(diff_blocks: List[Tuple[str, str]]) -> str:
    """
    Create a human-readable summary of the diff

    Args:
        diff_blocks: List of (search_text, replace_text) tuples

    Returns:
        Summary string
    """
    summary = []

    for i, (search_text, replace_text) in enumerate(diff_blocks):
        search_lines = search_text.strip().split("\n")
        replace_lines = replace_text.strip().split("\n")

        # Create a short summary
        if len(search_lines) == 1 and len(replace_lines) == 1:
            summary.append(f"Change {i+1}: '{search_lines[0]}' to '{replace_lines[0]}'")
        else:
            search_summary = (
                f"{len(search_lines)} lines" if len(search_lines) > 1 else search_lines[0]
            )
            replace_summary = (
                f"{len(replace_lines)} lines" if len(replace_lines) > 1 else replace_lines[0]
            )
            summary.append(f"Change {i+1}: Replace {search_summary} with {replace_summary}")

    return "\n".join(summary)


def calculate_edit_distance(code1: str, code2: str) -> int:
    """
    Calculate the Levenshtein edit distance between two code snippets

    Args:
        code1: First code snippet
        code2: Second code snippet

    Returns:
        Edit distance (number of operations needed to transform code1 into code2)
    """
    if code1 == code2:
        return 0

    # Simple implementation of Levenshtein distance
    m, n = len(code1), len(code2)
    dp = [[0 for _ in range(n + 1)] for _ in range(m + 1)]

    for i in range(m + 1):
        dp[i][0] = i

    for j in range(n + 1):
        dp[0][j] = j

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if code1[i - 1] == code2[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,  # deletion
                dp[i][j - 1] + 1,  # insertion
                dp[i - 1][j - 1] + cost,  # substitution
            )

    return dp[m][n]


def extract_code_language(code: str) -> str:
    """
    Try to determine the language of a code snippet

    Args:
        code: Code snippet

    Returns:
        Detected language or "unknown"
    """
    # Look for common language signatures
    if re.search(r"^(import|from|def|class)\s", code, re.MULTILINE):
        return "python"
    elif re.search(r"^(package|import java|public class)", code, re.MULTILINE):
        return "java"
    elif re.search(r"^(#include|int main|void main)", code, re.MULTILINE):
        return "cpp"
    elif re.search(r"^(function|var|let|const|console\.log)", code, re.MULTILINE):
        return "javascript"
    elif re.search(r"^(module|fn|let mut|impl)", code, re.MULTILINE):
        return "rust"
    elif re.search(r"^(SELECT|CREATE TABLE|INSERT INTO)", code, re.MULTILINE):
        return "sql"

    return "unknown"
