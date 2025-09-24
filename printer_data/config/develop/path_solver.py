EPS = 1e-6
AXES = ('x', 'y', 'z')

def nearly(a, b, eps=5e-4):
    return abs(a - b) <= eps

def overlaps(a0, a1, b0, b1):
    lo = max(min(a0, a1), b0)
    hi = min(max(a0, a1), b1)
    return hi >= lo - EPS

def clamp_check(value, lo, hi):
    return not (value < lo - EPS or value > hi + EPS)

def segment_hits(p0, p1, boxes):
    axis = None
    for ax in AXES:
        if not nearly(p0[ax], p1[ax]):
            axis = ax
            break
    if axis is None:
        return []
    hits = []
    for box in boxes:
        if axis == 'x':
            if (overlaps(p0['y'], p1['y'], box['min_y'], box['max_y'])
                and overlaps(p0['z'], p1['z'], box['min_z'], box['max_z'])
                and overlaps(p0['x'], p1['x'], box['min_x'], box['max_x'])):
                hits.append(box)
        elif axis == 'y':
            if (overlaps(p0['x'], p1['x'], box['min_x'], box['max_x'])
                and overlaps(p0['z'], p1['z'], box['min_z'], box['max_z'])
                and overlaps(p0['y'], p1['y'], box['min_y'], box['max_y'])):
                hits.append(box)
        else:
            if (overlaps(p0['x'], p1['x'], box['min_x'], box['max_x'])
                and overlaps(p0['y'], p1['y'], box['min_y'], box['max_y'])
                and overlaps(p0['z'], p1['z'], box['min_z'], box['max_z'])):
                hits.append(box)
    return hits

def attempt_vertical(current, target_z, boxes, limits):
    if nearly(current['z'], target_z):
        next_pt = dict(current); next_pt['z'] = target_z
        return next_pt, [], ''
    if not clamp_check(target_z, limits['min_z'], limits['max_z']):
        return None, [], 'Z target outside limits'
    attempt = dict(current); attempt['z'] = target_z
    hits = segment_hits(current, attempt, boxes)
    if hits:
        names = ','.join(box['name'] for box in hits)
        return None, [], f'Z move blocked by {names}'
    return attempt, [attempt], ''

def attempt_axis(current, axis, target_value, boxes, pad, limits):
    if nearly(current[axis], target_value):
        next_pt = dict(current); next_pt[axis] = target_value
        return next_pt, [], ''
    lo_key, hi_key = f'min_{axis}', f'max_{axis}'
    if not clamp_check(target_value, limits[lo_key], limits[hi_key]):
        return None, [], f'{axis.upper()} target outside limits'
    direct = dict(current); direct[axis] = target_value
    hits = segment_hits(current, direct, boxes)
    if not hits:
        return direct, [direct], ''
    other = 'y' if axis == 'x' else 'x'
    lo_key, hi_key = f'min_{other}', f'max_{other}'
    candidates = []
    pad = max(pad, 0.0)
    if other == 'y':
        top = max(box['max_y'] for box in hits)
        bottom = min(box['min_y'] for box in hits)
        if clamp_check(top + pad, limits[lo_key], limits[hi_key]): candidates.append(top + pad)
        if clamp_check(bottom - pad, limits[lo_key], limits[hi_key]): candidates.append(bottom - pad)
    else:
        right = max(box['max_x'] for box in hits)
        left = min(box['min_x'] for box in hits)
        if clamp_check(right + pad, limits[lo_key], limits[hi_key]): candidates.append(right + pad)
        if clamp_check(left - pad, limits[lo_key], limits[hi_key]): candidates.append(left - pad)
    candidates.sort(key=lambda v: abs(v - current[other]))
    for pivot_val in candidates:
        pivot = dict(current); pivot[other] = pivot_val
        if segment_hits(current, pivot, boxes): continue
        final_pt = dict(pivot); final_pt[axis] = target_value
        if segment_hits(pivot, final_pt, boxes): continue
        return final_pt, [pivot, final_pt], ''
    names = ','.join(sorted({box['name'] for box in hits}))
    return None, [], f'{axis.upper()} move blocked by {names}'

def deduplicate(points):
    cleaned = []
    for pt in points:
        rounded = {ax: round(float(pt[ax]), 3) for ax in AXES}
        if not cleaned or any(abs(rounded[ax] - cleaned[-1][ax]) > 5e-4 for ax in AXES):
            cleaned.append(rounded)
    return cleaned

def solve(data):
    start = {ax: float(data['start'][ax]) for ax in AXES}
    target = {ax: float(data['target'][ax]) for ax in AXES}
    travel_z = float(data['travel_z']) if data['travel_z'] is not None else None
    pad = float(data['pad'])
    boxes = data['obstacles']
    orders = data['orders']
    limits = data['limits']
    result = {'success': False, 'points': [], 'order': '', 'reason': 'no collision-free route'}
    for order in orders:
        current = dict(start)
        path = [dict(current)]
        if travel_z is not None and not nearly(current['z'], travel_z):
            best, pts, reason = attempt_vertical(current, travel_z, boxes, limits)
            if best is None:
                result['reason'] = reason
                continue
            path.extend(pts); current = best
        for ax in order:
            best, pts, reason = attempt_axis(current, ax, target[ax], boxes, pad, limits)
            if best is None:
                result['reason'] = reason or f'{ax.upper()} move blocked'
                break
            if pts: path.extend(pts)
            current = best
        else:
            if not nearly(current['x'], target['x']) or not nearly(current['y'], target['y']):
                result['reason'] = 'axis order left XY offset'
                continue
            if not nearly(current['z'], target['z']):
                best, pts, reason = attempt_vertical(current, target['z'], boxes, limits)
                if best is None:
                    result['reason'] = reason
                    continue
                if pts: path.extend(pts)
                current = best
            path[-1] = dict(target)
            result = {
                'success': True,
                'points': deduplicate(path),
                'order': '->'.join(ax.upper() for ax in order),
                'reason': ''
            }
            break
    return result
