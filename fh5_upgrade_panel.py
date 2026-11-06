"""Upgrade preferences embedded in the radar's main window."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from fh5_upgrade import (UpgradeCatalog, UpgradeService, load_preferences, save_preferences,
                         load_level10_preferences, save_level10_preferences)

LEVEL10_LABELS = {42:'RS200',43:'AMG ONE',44:'Centenario（牛）',45:'F9',46:'#25 Brocky'}


class UpgradePanel:
    def __init__(self, parent, source_getter=None, catalog=None, preference_path=None, service=None):
        self.catalog = catalog or UpgradeCatalog()
        self.preference_path = preference_path
        self.selections = load_preferences(self.catalog, preference_path)
        self.service = service or UpgradeService(source_getter, self.catalog)
        self.enabled = tk.BooleanVar(value=False)
        self.values = {}
        self.combos = {}
        self.frame = ttk.Frame(parent, padding=(20, 14))
        self.frame.pack(fill='both', expand=True)
        ttk.Label(self.frame, text='单挑升级选车', font=('Microsoft YaHei UI', 14, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(self.frame, text='每级指定一辆，或保持游戏随机。设置自动保存，重新开局后可继续使用。',
                  foreground='#526277').grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 8))
        self.enable_button = ttk.Checkbutton(self.frame, text='启用升级选车', variable=self.enabled, command=self.changed)
        self.enable_button.grid(row=2, column=0, columnspan=2, sticky='w', pady=(0, 8))
        if source_getter is None and service is None:
            self.enable_button.state(['disabled'])
        ttk.Button(self.frame, text='全部恢复随机', command=self.reset).grid(row=2, column=2, sticky='e', pady=(0, 8))
        ttk.Label(self.frame, text='目标等级', foreground='#64748b').grid(row=3, column=0, sticky='w')
        ttk.Label(self.frame, text='获胜后选择“升级车辆”时使用', foreground='#64748b').grid(row=3, column=1, columnspan=2, sticky='w')
        for level in range(2, 11):
            row = level+2
            ttk.Label(self.frame, text=f'升到 {level} 级').grid(row=row, column=0, sticky='w', padx=(0, 24), pady=4)
            options = ['保持随机']+[r['name'] for r in self.catalog.levels[level]]
            selected = self.selections[str(level)]
            variable = tk.StringVar(value=self.catalog.name(selected) if selected is not None else options[0])
            combo = ttk.Combobox(self.frame, state='readonly', textvariable=variable, values=options, width=58)
            combo.grid(row=row, column=1, columnspan=2, sticky='ew', pady=4)
            combo.bind('<<ComboboxSelected>>', self.changed)
            self.values[level], self.combos[level] = variable, combo
        ttk.Separator(self.frame).grid(row=13, column=0, columnspan=3, sticky='ew', pady=(12, 8))
        self.status = tk.StringVar(value='未启用；设置会自动保存' if source_getter else '离线预览：仅保存设置')
        self.current = tk.StringVar(value='本车：等待对局记录')
        self.next = tk.StringVar(value='下一辆：—')
        ttk.Label(self.frame, textvariable=self.status, foreground='#245b80', wraplength=800).grid(row=14, column=0, columnspan=3, sticky='w')
        ttk.Label(self.frame, textvariable=self.current, wraplength=800).grid(row=15, column=0, columnspan=3, sticky='w', pady=(5, 0))
        ttk.Label(self.frame, textvariable=self.next, wraplength=800).grid(row=16, column=0, columnspan=3, sticky='w', pady=(2, 6))
        ttk.Label(self.frame, text='6→7、8→9 已有联机与画面核对记录，9→10 有运行时验证。\n10级同级换车请在对应页单独设置；跨级升级未接入。本页不设置开局的1级车。',
                  foreground='#687381', wraplength=820).grid(row=17, column=0, columnspan=3, sticky='w')
        self.frame.columnconfigure(1, weight=1)
        self.frame.columnconfigure(2, weight=1)

    def changed(self, event=None):
        self.selections = {str(n): next((r['key'] for r in self.catalog.levels[n]
                                        if r['name'] == self.values[n].get()), None) for n in range(2, 11)}
        try:
            save_preferences(self.catalog, self.selections, self.preference_path)
        except OSError as error:
            self.enabled.set(False)
            self.status.set(f'设置保存失败：{error}')
            self.service.configure(False, self.selections)
            return
        self.service.configure(self.enabled.get(), self.selections)
        self.status.set('正在接入升级记录…' if self.enabled.get() else '设置已保存；选车未启用')

    def reset(self):
        for variable in self.values.values():
            variable.set('保持随机')
        self.changed()

    def tick(self):
        state = self.service.snapshot()
        if self.enabled.get() and not state.get('normal_enabled', state['enabled']):
            self.enabled.set(False)
        # Preserve a save error until the next user action.
        if not self.status.get().startswith('设置保存失败'):
            self.status.set(state['text'])
        chain = state.get('chain') or []
        self.current.set(f"本车：{chain[0]['level']} 级 · {self.catalog.name(chain[0]['key'])}" if chain else '本车：等待对局记录')
        self.next.set(f"下一辆：{chain[1]['level']} 级 · {self.catalog.name(chain[1]['key'])}" if len(chain) > 1 else '下一辆：—')

    def close(self):
        self.service.close()


class Level10Panel:
    def __init__(self,parent,source_getter=None,catalog=None,preference_path=None,service=None):
        self.catalog=catalog or UpgradeCatalog()
        self.preference_path=preference_path
        self.rules=load_level10_preferences(self.catalog,preference_path)
        self.owns_service=service is None
        self.service=service or UpgradeService(source_getter,self.catalog)
        self.enabled=tk.BooleanVar(value=False)
        self.values={};self.combos={};self.option_rows={}
        self.frame=ttk.Frame(parent,padding=(20,14));self.frame.pack(fill='both',expand=True)
        ttk.Label(self.frame,text='10级单挑换车规则',font=('Microsoft YaHei UI',14,'bold')).grid(row=0,column=0,columnspan=3,sticky='w')
        ttk.Label(self.frame,text='按当前车型分别指定下一辆10级车；“保留原规则”表示不干预该车型。',foreground='#526277',wraplength=820).grid(row=1,column=0,columnspan=3,sticky='w',pady=(5,10))
        self.enable_button=ttk.Checkbutton(self.frame,text='启用10级换车规则（实验）',variable=self.enabled,command=self.changed)
        self.enable_button.grid(row=2,column=0,columnspan=2,sticky='w',pady=(0,12))
        if source_getter is None:self.enable_button.state(['disabled'])
        ttk.Button(self.frame,text='全部保留原规则',command=self.reset).grid(row=2,column=2,sticky='e',pady=(0,12))
        ttk.Label(self.frame,text='当前开的10级车',foreground='#64748b').grid(row=3,column=0,sticky='w',pady=(0,4))
        ttk.Label(self.frame,text='获胜后想换成',foreground='#64748b').grid(row=3,column=1,columnspan=2,sticky='w',pady=(0,4))
        for i,row in enumerate(self.catalog.levels[10]):
            key=row['key'];target=self.rules[str(key)]
            ttk.Label(self.frame,text=LEVEL10_LABELS[key]).grid(row=i+4,column=0,sticky='w',padx=(0,22),pady=10)
            candidates=[r for r in self.catalog.levels[10] if r['key']!=key]
            self.option_rows[key]=candidates
            variable=tk.StringVar(value=self.catalog.name(target) if target is not None else '保留原规则')
            combo=ttk.Combobox(self.frame,state='readonly',textvariable=variable,
                               values=['保留原规则']+[r['name'] for r in candidates],width=60)
            combo.grid(row=i+4,column=1,columnspan=2,sticky='ew',pady=10)
            combo.bind('<<ComboboxSelected>>',self.changed)
            self.values[key]=variable;self.combos[key]=combo
        ttk.Separator(self.frame).grid(row=9,column=0,columnspan=3,sticky='ew',pady=(16,12))
        self.status=tk.StringVar(value='未启用；每辆车的规则会分别保存')
        self.current=tk.StringVar(value='本车：等待对局记录')
        self.target=tk.StringVar(value='本车规则：—')
        self.pending=tk.StringVar(value='游戏待换车辆：—')
        for line,var in enumerate([self.status,self.current,self.target,self.pending],10):
            ttk.Label(self.frame,textvariable=var,foreground='#245b80' if line==10 else '#25364a',wraplength=820).grid(row=line,column=0,columnspan=3,sticky='w',pady=3)
        ttk.Label(self.frame,text='实验功能，10→10尚未完成联机实测；只改游戏已经生成的待换车辆记录。\n等状态显示“10级目标已准备”后，获胜时选择游戏中的换车选项。\n这里的规则与“升级选车”页中9→10的选择独立，不能选择自己或跨等级车辆。',
                  foreground='#687381',wraplength=820).grid(row=14,column=0,columnspan=3,sticky='w',pady=(14,0))
        self.frame.columnconfigure(1,weight=1);self.frame.columnconfigure(2,weight=1)

    def changed(self,event=None):
        self.rules={str(key):next((r['key'] for r in self.option_rows[key] if r['name']==value.get()),None)
                    for key,value in self.values.items()}
        try:save_level10_preferences(self.catalog,self.rules,self.preference_path)
        except OSError as error:
            self.enabled.set(False);self.service.configure_level10(False,self.rules)
            self.status.set(f'规则保存失败：{error}');return
        self.service.configure_level10(self.enabled.get(),self.rules)
        self.status.set('正在接入10级换车记录…' if self.enabled.get() else '规则已保存；10级改选未启用')

    def reset(self):
        for value in self.values.values():value.set('保留原规则')
        self.changed()

    def tick(self):
        state=self.service.snapshot()
        if self.enabled.get() and not state.get('level10_enabled',False):self.enabled.set(False)
        if not self.status.get().startswith('规则保存失败'):
            show_reason=self.enabled.get() or state['text'].startswith('已暂停：')
            self.status.set(state['text'] if show_reason else '未启用；每辆车的规则会分别保存')
        chain=state.get('chain') or []
        if not chain:
            self.current.set('本车：等待对局记录');self.target.set('本车规则：—');self.pending.set('游戏待换车辆：—');return
        current=chain[0]
        self.current.set(f"本车：{current['level']}级 · {self.catalog.name(current['key'])}")
        key=self.rules.get(str(current['key'])) if current['level']==10 else None
        self.target.set('本车规则：'+(self.catalog.name(key) if key is not None else '保留原规则' if current['level']==10 else '到达10级后使用'))
        self.pending.set('游戏待换车辆：'+(self.catalog.name(chain[1]['key']) if len(chain)>1 and current['level']==10 else '等待游戏生成'))

    def close(self):
        if self.owns_service:self.service.close()
