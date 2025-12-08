import jinja2
from pathlib import Path
loader = jinja2.FileSystemLoader('templates')
env = jinja2.Environment(loader=loader)
env.get_template('profile_detail.html')
print('compiled')
