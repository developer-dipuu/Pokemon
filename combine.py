import os

output_file = "combined_code.txt"
# Added 'asset' and 'server' (server was already there, but verified)
ignore_dirs = {'server', 'asset', 'assets', '__pycache__', '.venv', 'venv', 'env', '.git', 'node_modules'}

def generate_tree(dir_path, prefix=""):
    tree_str = ""
    try:
        items = os.listdir(dir_path)
    except PermissionError:
        return ""
    
    dirs, files = [], []
    for item in items:
        path = os.path.join(dir_path, item)
        if os.path.isdir(path):
            if item not in ignore_dirs:
                dirs.append(item)
        else:
            # Logic: Ignore output file, script, .pyc files, AND .png files
            is_ignored_file = (
                item in (output_file, 'combine.py') or 
                item.endswith('.pyc') or 
                item.endswith('.png')
            )
            if not is_ignored_file:
                files.append(item)
    
    dirs.sort()
    files.sort()
    all_items = dirs + files
    
    for i, item in enumerate(all_items):
        is_last = (i == len(all_items) - 1)
        connector = "└── " if is_last else "├── "
        tree_str += f"{prefix}{connector}{item}\n"
        
        if item in dirs:
            extension = "    " if is_last else "│   "
            tree_str += generate_tree(os.path.join(dir_path, item), prefix + extension)
            
    return tree_str

with open(output_file, 'w', encoding='utf-8') as outfile:
    # 1. Gather and write all Python files
    for root, dirs, files in os.walk('.'):
        # Modify dirs in-place to skip ignored directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        
        for file in files:
            # Skip .png and specific script files here too
            if file.endswith('.py') and file != 'combine.py' and not file.endswith('.png'):
                file_path = os.path.join(root, file)
                outfile.write(f"\n========== {file_path} ==========\n\n")
                try:
                    with open(file_path, 'r', encoding='utf-8') as infile:
                        outfile.write(infile.read())
                        outfile.write("\n")
                except Exception as e:
                    outfile.write(f"# Could not read file: {e}\n")
    
    # 2. Generate and write the directory tree
    outfile.write("\n========== DIRECTORY TREE ==========\n\n")
    outfile.write(".\n")
    outfile.write(generate_tree('.'))

print(f"Success! All files and the tree have been written to {output_file}")