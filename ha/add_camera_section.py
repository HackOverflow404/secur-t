import json, sys

path = sys.argv[1]
doc = json.load(open(path))
views = doc['data']['config']['views']
home = next(v for v in views if v.get('path') == 'home')
if any(c.get('heading') == 'SECUR-T' for s in home['sections'] for c in s.get('cards', [])):
    sys.exit('SECUR-T section already present')

CAM = 'camera.bedroom_secur_t'
section = {
    'type': 'grid',
    'cards': [
        {'type': 'heading', 'heading': 'SECUR-T', 'icon': 'mdi:cctv', 'heading_style': 'title'},
        {
            'type': 'picture-entity',
            'entity': CAM,
            'camera_view': 'live',
            'show_name': False,
            'show_state': False,
            'tap_action': {'action': 'more-info'},
            'card_mod': {'style': 'ha-card {\n  border-radius: 25px;\n  overflow: hidden;\n}\n'},
            'grid_options': {'columns': 12},
        },
        {
            'type': 'tile',
            'entity': CAM,
            'name': 'Recordings',
            'icon': 'mdi:filmstrip-box-multiple',
            'color': 'red',
            'hide_state': True,
            'tap_action': {'action': 'navigate', 'navigation_path': '/media-browser/browser'},
            'icon_tap_action': {'action': 'navigate', 'navigation_path': '/media-browser/browser'},
            'grid_options': {'columns': 12, 'rows': 1},
        },
    ],
}
clippy_index = next(i for i, s in enumerate(home['sections'])
                    if any(c.get('heading') == 'Clippy' for c in s.get('cards', [])))
home['sections'].insert(clippy_index + 1, section)
json.dump(doc, open(path, 'w'), indent=2, ensure_ascii=False)
print('inserted at section', clippy_index + 1)
