"""Calibrated clan formation reader, sharing the existing portrait and HUD checks."""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from maa.custom_recognition import CustomRecognition

from .guide_trial_characters import GuideTrialCharacters
from .labyrinth.character_recognition import load_character_pack


def box_inside_roi(box, roi):
    return (type(box) is list and len(box) == 4 and type(roi) in (list, tuple) and len(roi) == 4
        and all(type(value) is int for value in (*box, *roi))
        and box[2] > 0 and box[3] > 0 and roi[2] > 0 and roi[3] > 0
        and box[0] >= roi[0] and box[1] >= roi[1]
        and box[0] + box[2] <= roi[0] + roi[2] and box[1] + box[3] <= roi[1] + roi[3])


class ClanBattleCharacters(GuideTrialCharacters):
    def __init__(self, plan, calibration, page):
        # The deep-domain constructor also authorizes a particular source/stage.
        CustomRecognition.__init__(self)
        self.plan, self.team, self.calibration, self.page = plan, plan['unit_ids'], calibration, page
        entries, images, _ = load_character_pack()
        self.templates = {uid: [] for uid in self.team}
        for entry, rgb in zip(entries, images):
            uid = int(entry['character_id']) * 100 + 1
            if uid not in self.templates:
                continue
            for size in (124, 126, 128):
                icon = cv2.resize(rgb[:, :, ::-1], (size, size))
                x, y, right, bottom = int(size*.12), int(size*.22), int(size*.82), int(size*.68)
                self.templates[uid].append((icon[y:bottom, x:right].copy(), x, y, size))
        if any(not values for values in self.templates.values()):
            raise RuntimeError('clan_battle_character_resources_missing')
        root = Path(__file__).resolve().parents[2] / 'assets/resource/image/guide-trial'
        self.controls = {name: cv2.imdecode(np.frombuffer((root / f'battle-{name}.png').read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                         for name in ('auto-on', 'auto-off', 'set-on', 'set-off', 'set-badge', 'set-border-off')}
        if any(image is None for image in self.controls.values()):
            raise RuntimeError('clan_battle_control_resources_missing')
        self.badge_names = []
        for width in range(34, 49):
            name = f'set-badge-{width}'
            self.controls[name] = cv2.resize(self.controls['set-badge'], (width, width+1))
            self.badge_names.append(name)

    def formation(self, context, image):
        identity = self.page.read(context, image, 'formation')
        layout = self.calibration['formation']
        account_stars = None
        if layout.get('star_source') == 'account_snapshot':
            from .clan_battle import account_battle_stars
            account_stars = account_battle_stars(self.plan)
        members, stars, support, boxes = [], [], [], []
        occupied_count = 0
        for index, roi in enumerate(layout['slots']):
            hits = [row for uid in self.team if (row := self.match(image, uid, roi))]
            empty = self.page.hit(context, image, f'CBEmpty{index}')
            is_support = self.page.hit(context, image, f'CBSupport{index}')
            is_owned = (not empty and not is_support) if self.calibration.get('version') == 2 else self.page.hit(context, image, f'CBOwned{index}')
            occupied = is_support != is_owned
            if len(hits) > 1 or hits and empty or is_support and is_owned or empty == occupied:
                raise RuntimeError('clan_battle_formation_ambiguous')
            if not hits and not empty and not occupied:
                raise RuntimeError('clan_battle_formation_slot_unknown')
            members.append(hits[0]['unit_id'] if hits else None)
            boxes.append(hits[0]['box'] if hits else roi if occupied else None)
            if occupied:
                occupied_count += 1
                if account_stars is not None and hits and not is_support:
                    stars.append(account_stars[hits[0]['unit_id']])
                else:
                    values = [star for star in range(1, 7) if self.page.hit(context, image, f'CBStar{index}_{star}')]
                    stars.append(values[0] if len(values) == 1 else None)
                support.append(hits[0]['unit_id'] if hits and is_support else None)
            else:
                stars.append(None)
                support.append(None)
        chosen = [uid for uid in members if uid is not None]
        if len(set(chosen)) != len(chosen) or sum(uid is not None for uid in support) > 1:
            raise RuntimeError('clan_battle_formation_ambiguous')
        return dict(identity, unit_ids=members, stars=stars, member_count=occupied_count, boxes=boxes,
                    support_unit_id=next((uid for uid in support if uid is not None), None),
                    stars_source='account_snapshot' if account_stars is not None else 'screen')

    def support_candidates(self, context, image):
        identity = self.page.read(context, image, 'support')
        candidates = []
        for index, roi in enumerate(self.calibration['support']['slots']):
            hits = [row for uid in self.team if (row := self.match(image, uid, roi))]
            if len(hits) > 1:
                raise RuntimeError('clan_battle_support_ambiguous')
            if not hits:
                continue
            if not box_inside_roi(hits[0]['box'], roi):
                continue
            stars = [star for star in range(1, 7) if self.page.hit(context, image, f'CBSupportStar{index}_{star}')]
            if len(stars) != 1:
                continue
            identifier = self.page.word(context, image, f'CBSupportId{index}')
            available = self.page.hit(context, image, f'CBSupportAvailable{index}')
            unavailable = self.page.hit(context, image, f'CBSupportUnavailable{index}')
            if available == unavailable:
                continue
            candidates.append(dict(unit_id=hits[0]['unit_id'], stars=stars[0], support_id=identifier,
                                   available=available, box=hits[0]['box']))
        return dict(identity, candidates=candidates)

    def battle_control(self, context, argv, params):
        page = self.page.read(context, argv.image, 'battle')
        if type(page.get('remaining_seconds')) is not int or not 0 <= page['remaining_seconds'] <= 90:
            return None
        if 'expected_remaining_seconds' in params:
            target_second = params['expected_remaining_seconds']
            if (type(target_second) is not int or not 0 <= target_second <= 89
                    or page['remaining_seconds'] != target_second
                    or getattr(self, 'calibration', {}).get('version') == 2
                    and (type(page.get('battle_speed')) is not int or page['battle_speed'] != 1)):
                return None
        state = self.read_controls(argv.image)
        if (type(state.get('auto')) is not bool or len(state.get('sets', [])) != 5
                or any(type(value) is not bool for value in state['sets'])
                or set(state.get('unit_ids', [])) != set(self.team)
                or len(set(state['unit_ids'])) != 5):
            return None
        for key in ('auto', 'sets', 'unit_ids'):
            if key in params and state[key] != params[key]:
                return None
        if 'battle_speed' in params and page.get('battle_speed') != params['battle_speed']:
            return None
        mode, target = params['mode'], [0, 0, 1, 1]
        if mode == 'battle_auto':
            if (type(params.get('previous')) is not bool or type(params.get('desired')) is not bool
                    or params['previous'] is params['desired'] or state['auto'] is not params['previous']):
                return None
            target = [1190, 528, 55, 55]
        elif mode == 'battle_set':
            slot = params.get('slot')
            if (type(slot) is not int or not 0 <= slot < 5 or type(params.get('previous')) is not bool
                    or type(params.get('desired')) is not bool or params['previous'] is params['desired']
                    or state['sets'][slot] is not params['previous']):
                return None
            target = [280+160*slot, 560, 75, 65]
        elif mode != 'battle_read':
            return None
        if context.tasker.stopping:
            return None
        state.update(remaining_seconds=page['remaining_seconds'], same_frame=True, source='maa',
            frame_pixel_sha256=hashlib.sha256(argv.image.tobytes()).hexdigest())
        if getattr(self, 'calibration', {}).get('version') == 2:
            state['battle_speed'] = page.get('battle_speed')
        return self.AnalyzeResult(box=target, detail=state)

    def read_paused_controls(self, context, image, *, prefix, remaining_seconds, speeds=(1, 2, 4)):
        for i in range(5):
            if not self.page.hit(context, image, prefix+'EmptyTp'+str(i)):
                return None
            tp = cv2.cvtColor(image[385:392, 363+116*i:452+116*i], cv2.COLOR_BGR2HSV)
            if ((tp[:,:,0] >= 80) & (tp[:,:,0] <= 120) & (tp[:,:,1] >= 100) & (tp[:,:,2] >= 130)).any():
                return None
        members = self.read_paused(image)
        ids = [row['unit_id'] if row else None for row in members]
        if len(set(ids)) != 5 or set(ids) != set(self.team):
            return None
        sets, evidence = [], []
        for i in range(5):
            hsv = cv2.cvtColor(image[286:346, 358+116*i:363+116*i], cv2.COLOR_BGR2HSV)
            cyan = float(((hsv[:,:,0]>=75)&(hsv[:,:,0]<=105)&(hsv[:,:,1]>=70)&(hsv[:,:,2]>=100)).mean())
            red = float((((hsv[:,:,0]<=15)|(hsv[:,:,0]>=170))&(hsv[:,:,1]>=70)&(hsv[:,:,2]>=100)).mean())
            badge = self.page.hit(context, image, prefix+'Set'+str(i))
            sets.append(True if badge and cyan >= .6 else False if not badge and cyan < .2 and red >= .6 else None)
            evidence.append(dict(badge=badge, cyan_border=cyan, red_border=red))
        auto = [value for value, label in ((True, 'on'), (False, 'off'))
                if self.page.hit(context, image, prefix+'_auto_'+label)]
        speeds = [speed for speed in speeds if self.page.hit(context, image, prefix+'Speed'+str(speed))]
        if any(value is None for value in sets) or len(auto) != 1 or len(speeds) != 1:
            return None
        state = dict(unit_ids=ids, sets=sets, auto=auto[0], battle_speed=speeds[0], paused=True,
            remaining_seconds=remaining_seconds, zero_damage_verified=True, zero_tp_verified=True,
            same_frame=True, source='maa', portrait_evidence=members, slot_evidence=evidence,
            frame_pixel_sha256=hashlib.sha256(image.tobytes()).hexdigest())
        return state

    def opening_control(self, context, argv, params):
        spec = self.calibration.get('auto_battle')
        mode = params['mode']
        if not spec or self.plan.get('switches') or self.page.current_page != 'battle':
            return None
        if self.page.hit(context, argv.image, 'LabyDisconnect6002Observed'):
            return None
        if not all(self.page.hit(context, argv.image, f'CBOpening_paused_guards_{i}') for i in range(4)):
            return None
        if not self.page.hit(context, argv.image, 'CBOpeningBoss'):
            return None
        seconds = [second for second in (90, 89) if self.page.hit(context, argv.image, 'CBOpeningClock'+str(second))]
        if len(seconds) != 1:
            return None
        state = self.read_paused_controls(context, argv.image, prefix='CBOpening', remaining_seconds=seconds[0])
        if state is None:
            return None
        if mode != 'opening_read' and not all(key in params for key in ('unit_ids', 'sets', 'auto', 'battle_speed')):
            return None
        if 'battle_speed' in params and type(params['battle_speed']) is not int:
            return None
        if any(key in params and state[key] != params[key] for key in ('unit_ids', 'sets', 'auto', 'battle_speed')):
            return None
        # This menu layout is calibrated with the 1280x720 clan HUD; every click is read back while paused.
        target = [0, 0, 1, 1]
        if mode == 'opening_auto':
            if (type(params.get('previous')) is not bool or type(params.get('desired')) is not bool
                    or params['previous'] is params['desired'] or state['auto'] is not params['previous']):
                return None
            target = [634, 473, 6, 6]
        elif mode == 'opening_set':
            slot = params.get('slot')
            if (type(slot) is not int or not 0 <= slot < 5 or type(params.get('previous')) is not bool
                    or type(params.get('desired')) is not bool or params['previous'] is params['desired']
                    or state['sets'][slot] is not params['previous']):
                return None
            target = [403+116*slot, 311, 6, 6]
        elif mode == 'opening_speed':
            target = [830, 473, 6, 6]
        elif mode == 'opening_resume':
            if not all(key in params for key in ('unit_ids', 'sets', 'auto', 'battle_speed')):
                return None
            target = [442, 574, 6, 6]
        elif mode != 'opening_read':
            return None
        if context.tasker.stopping:
            return None
        return self.AnalyzeResult(box=target, detail=state)

    def analyze(self, context, argv):
        try:
            if context.tasker.stopping or argv.image is None or argv.image.shape != (720, 1280, 3):
                return None
            params = json.loads(argv.custom_recognition_param)
            mode = params.get('mode')
            if str(mode).startswith('opening_'):
                return self.opening_control(context, argv, params)
            if str(mode).startswith('battle_'):
                return self.battle_control(context, argv, params)
            if mode in ('support_read', 'support_add'):
                detail = self.support_candidates(context, argv.image)
                target = [0, 0, 1, 1]
                if mode == 'support_add':
                    chosen = [row for row in detail['candidates'] if row['support_id'] == params.get('support_id')
                              and row['unit_id'] == params.get('unit_id') and row['stars'] == params.get('stars')
                              and row['available'] is True]
                    if len(chosen) != 1:
                        return None
                    target = chosen[0]['box']
                    if not any(box_inside_roi(target, roi) for roi in self.calibration['support']['slots']):
                        return None
            else:
                detail = self.formation(context, argv.image)
                target = [0, 0, 1, 1]
                if mode == 'clear':
                    if detail['member_count'] != params.get('count') or not detail['member_count']:
                        return None
                    target = next(box for box in reversed(detail['boxes']) if box)
                elif mode == 'add':
                    uid = params.get('unit_id')
                    if uid not in self.team or uid in detail['unit_ids'] or detail['member_count'] >= 5:
                        return None
                    hits = [(row, roi) for roi in self.calibration['formation']['candidates']
                            if (row := self.match(argv.image, uid, roi))]
                    if len(hits) != 1 or not box_inside_roi(hits[0][0]['box'], hits[0][1]):
                        return None
                    target = hits[0][0]['box']
                elif mode == 'start':
                    self.page.require_team(detail)
                    target = self.page.action(context, argv.image, 'start')
                    if self.calibration.get('auto_battle') and not self.plan['switches']:
                        self.page.current_page, self.page.expected_page = 'battle', None
                elif mode != 'read':
                    return None
            if mode in ('clear', 'add', 'support_add'):
                x, y, width, height = target
                target = [x+width//2-3, y+height//2-3, 6, 6]
            if context.tasker.stopping:
                return None
            return self.AnalyzeResult(box=target, detail=detail)
        except Exception:
            return None
