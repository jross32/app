import sys
import traceback

try:
    import app
    print('Imported app.py successfully.')
    if hasattr(app, 'app'):
        print('Flask app object found.')
    else:
        print('No Flask app object named "app" found in app.py.')
except Exception as e:
    print('Error importing app.py:')
    traceback.print_exc()
    sys.exit(1)

try:
    if hasattr(app, 'app'):
        print('Attempting to run app.app.run(debug=True)')
        app.app.run(debug=True)
    else:
        print('No Flask app object to run.')
except Exception as e:
    print('Error running app:')
    traceback.print_exc()
    sys.exit(2)
