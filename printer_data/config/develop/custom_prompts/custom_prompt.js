        (function () {
            var C = (function(){ try { var c = window.__psd_cfg; delete window.__psd_cfg; return c || {}; } catch(e){ return {}; } })();
            if (!C || typeof C !== 'object') return;

            function spawnDialog(cfg) {
                if (document.getElementById(cfg.rootId)) {
                    var existingDlg = document.getElementById(cfg.rootId).querySelector('.psd-dlg');
                    if (existingDlg) {
                         var maxZ = Array.from(document.querySelectorAll('.psd-dlg')).reduce((max, dlg) => Math.max(max, parseInt(dlg.style.zIndex || 202)), 201);
                         existingDlg.style.zIndex = maxZ + 1;
                    }
                    return;
                }
                function imp(el, prop, val) { try { el.style.setProperty(prop, val, 'important'); } catch(_) { try { el.style[prop]=val; } catch(__){} } }
                function clamp(v,min,max){ return Math.max(min, Math.min(max, v)); }
                var root=document.createElement('div');
                root.id=C.rootId;
                imp(root,'position','fixed'); imp(root,'left','0'); imp(root,'top','0'); imp(root,'width','0'); imp(root,'height','0'); imp(root,'pointer-events','none'); imp(root,'z-index','202');
                document.body.appendChild(root);
                if (!document.getElementById(C.styleId)) {
                    var st=document.createElement('style'); st.id=C.styleId;
                    st.textContent =
                    ".psd-dlg{pointer-events:auto;position:fixed;box-sizing:border-box;outline:none;}"+
                    ".psd-card{position:relative;width:100%;height:100%;border-radius:8px;border:1px solid var(--p-border);box-shadow:none;display:flex;flex-direction:column;box-sizing:border-box;overflow:hidden;}"+
                    ".psd-toolbar{height:48px;flex:none;border-bottom:1px solid var(--p-border);box-sizing:border-box;}"+
                    ".psd-toolbar>.v-toolbar__content{height:48px;display:flex;align-items:center;gap:8px;padding:0 8px;box-sizing:border-box;white-space:nowrap;overflow:hidden;}"+
                    ".psd-toolbar .v-toolbar__items{margin:0;padding:0;}"+
                    ".psd-title{display:flex;align-items:center;gap:8px;min-width:0;}"+
                    ".psd-spacer{flex:1 1 auto;}"+
                    ".psd-close{min-width:0;margin:0;padding:0;width:40px;height:40px;display:inline-flex;align-items:center;justify-content:center;}"+
                    ".psd-body-wrap{flex:1 1 auto;min-height:0;display:flex;}"+
                    ".psd-body{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;overflow:auto;overscroll-behavior:contain;padding:12px;}"+
                    ".psd-actions{flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:8px;border-top:1px solid var(--p-border);box-sizing:border-box;}"+
                    ".psd-body::-webkit-scrollbar{width:0;height:0;} .psd-body{-ms-overflow-style:none;scrollbar-width:none;}"+
                    ".psd-embed-fit{flex:1 1 auto;min-height:0;max-height:100%;display:flex;}"+
                    ".psd-embed-fit>*{flex:1 1 auto;min-height:0;max-height:100%;}"+
                    ".psd-grip{position:absolute;z-index:5;background:transparent;}"+
                    ".psd-grip-t{top:-5px;left:0;width:100%;height:10px;cursor:n-resize;}"+
                    ".psd-grip-b{bottom:-5px;left:0;width:100%;height:10px;cursor:s-resize;}"+
                    ".psd-grip-l{top:0;left:-5px;width:10px;height:100%;cursor:w-resize;}"+
                    ".psd-grip-r{top:0;right:-5px;width:10px;height:100%;cursor:e-resize;}"+
                    ".psd-grip-tl{top:-6px;left:-6px;width:18px;height:18px;cursor:nwse-resize;z-index:6;}"+
                    ".psd-grip-tr{top:-6px;right:-6px;width:18px;height:18px;cursor:nesw-resize;z-index:6;}"+
                    ".psd-grip-bl{bottom:-6px;left:-6px;width:18px;height:18px;cursor:nesw-resize;z-index:6;}"+
                    ".psd-grip-br{bottom:-6px;right:-6px;width:18px;height:18px;cursor:nwse-resize;z-index:6;}"+
                    ".psd-grab{cursor:move;}";
                    document.head.appendChild(st);
                }
                var dlg=document.createElement('div'); dlg.className='psd-dlg'; dlg.setAttribute('tabindex','0');
                var card=document.createElement('div'); card.className='v-card v-sheet theme--dark panel psd-card v-card--raised macro_prompt-dialog';
                var header=document.createElement('header'); header.className='panel psd-toolbar v-sheet theme--dark v-toolbar v-toolbar--dense v-toolbar--flat psd-grab';
                var hc=document.createElement('div'); hc.className='v-toolbar__content';
                var titleWrap=document.createElement('div'); titleWrap.className='v-toolbar__title d-flex align-center psd-title';
                var iconSpan=document.createElement('span'); iconSpan.setAttribute('aria-hidden','true'); iconSpan.className='v-icon notranslate v-icon--left theme--dark';
                iconSpan.innerHTML='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" role="img" aria-hidden="true" class="v-icon__svg"><path d="M13,9H11V7H13M13,17H11V11H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z"></path></svg>';
                var titleSpan=document.createElement('span'); titleSpan.className='subheading'; titleSpan.textContent=String(C.title||'');
                var spacer=document.createElement('div'); spacer.className='psd-spacer';
                var items=document.createElement('div'); items.className='v-toolbar__items';
                var itemsFlex=document.createElement('div'); itemsFlex.className='d-flex align-center';
                var xBtn=document.createElement('button'); xBtn.type='button'; xBtn.className='v-btn v-btn--icon v-btn--round v-btn--tile theme--dark v-size--default psd-close'; xBtn.setAttribute('aria-label','Close');
                var xbContent=document.createElement('span'); xbContent.className='v-btn__content';
                var xIcon=document.createElement('span'); xIcon.setAttribute('aria-hidden','true'); xIcon.className='v-icon notranslate theme--dark';
                xIcon.innerHTML='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" role="img" aria-hidden="true" class="v-icon__svg"><path d="M20 6.91L17.09 4L12 9.09L6.91 4L4 6.91L9.09 12L4 17.09L6.91 20L12 14.91L17.09 20L20 17.09L14.91 12L20 6.91Z"></path></svg>';
                xbContent.appendChild(xIcon); xBtn.appendChild(xbContent);
                titleWrap.appendChild(iconSpan); titleWrap.appendChild(titleSpan);
                hc.appendChild(titleWrap); hc.appendChild(spacer);
                itemsFlex.appendChild(xBtn); items.appendChild(itemsFlex); hc.appendChild(items);
                header.appendChild(hc);
                var bodyWrap=document.createElement('div'); bodyWrap.className='psd-body-wrap';
                var body=document.createElement('div'); body.className='v-card__text psd-body';
                bodyWrap.appendChild(body);
                var actions=document.createElement('div'); actions.className='v-card__actions psd-actions';
                var actionsSpacer=document.createElement('div'); actionsSpacer.className='psd-spacer';
                var okBtn = document.createElement('button'); okBtn.type = 'button'; okBtn.className = 'v-btn v-btn--text theme--dark v-size--default primary--text';
                var okBtnContent = document.createElement('span'); okBtnContent.className = 'v-btn__content'; okBtnContent.textContent = 'OK';
                okBtn.appendChild(okBtnContent);
                actions.appendChild(actionsSpacer); actions.appendChild(okBtn);
                card.appendChild(header); card.appendChild(bodyWrap); card.appendChild(actions);
                var grips = {};
                ['t','b','l','r','tl','tr','bl','br'].forEach(function(dir) {
                    var g = document.createElement('div');
                    g.className = 'psd-grip psd-grip-' + dir;
                    card.appendChild(g);
                    grips[dir] = g;
                });
                dlg.appendChild(card); root.appendChild(dlg);
                var bg = String(C.bg||'rgb(30,31,34)');
                imp(card,'background-color',bg); imp(header,'background-color',bg); imp(body,'background-color',bg); imp(actions,'background-color',bg);
                try {
                    var html=atob(C.html_b64||'');
                    var wrap=document.createElement('div'); wrap.className='psd-embed-fit';
                    body.appendChild(wrap);
                    wrap.insertAdjacentHTML('afterbegin', html);
                } catch(_) { body.textContent='HTML payload decode/insert failed.'; }
                function closeIt(){ try{root.remove()}catch(_){ } try{window.__psd_gate.dismissed[String(C.ts)]=1}catch(_){ } }
                xBtn.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); closeIt(); }, false);
                okBtn.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); closeIt(); }, false);
                document.addEventListener('keydown', function(e){ if(e.key==='Escape') closeIt(); }, false);
                var minW = Math.max(280, C.minW|0), minH = Math.max(160, C.minH|0);
                var wantW=Math.max(C.wantW|0, minW);
                var sw = window.innerWidth, sh = window.innerHeight;
                var maxW = Math.max(minW, sw - 48), maxH = Math.max(minH, sh - 48);
                var W = clamp(wantW, minW, maxW), H = clamp(Math.floor(sh*0.6), minH, maxH);
                imp(dlg,'width',  W+'px'); imp(dlg,'height', H+'px');
                dlg.style.left = Math.max(0, Math.floor((sw - W)/2)) + 'px';
                dlg.style.top  = Math.max(0, Math.floor((sh - H)/2)) + 'px';
                var isDragging=false, isResizing=false;
                function isInteractive(el){ return el.closest && el.closest('button,a,input,textarea,select,[role="button"]'); }
                header.addEventListener('pointerdown', function(ev){
                    if (ev.button !== 0 || isResizing || isInteractive(ev.target)) return;
                    ev.preventDefault();
                    var r = dlg.getBoundingClientRect();
                    var ox = ev.clientX - r.left, oy = ev.clientY - r.top;
                    isDragging = true;
                    dlg.setPointerCapture(ev.pointerId);
                    function onMove(e){
                        if (!isDragging) return;
                        var L = clamp(e.clientX - ox, 0, window.innerWidth - r.width);
                        var T = clamp(e.clientY - oy, 0, window.innerHeight - r.height);
                        dlg.style.left = L + 'px';
                        dlg.style.top = T + 'px';
                    }
                    function onUp(e){
                        isDragging = false;
                        try { dlg.releasePointerCapture(e.pointerId); } catch(_){}
                        dlg.removeEventListener('pointermove', onMove);
                        dlg.removeEventListener('pointerup', onUp);
                    }
                    dlg.addEventListener('pointermove', onMove);
                    dlg.addEventListener('pointerup', onUp);
                });
                function startResize(ev, mode){
                    if (ev.button !== 0 || isDragging) return;
                    ev.preventDefault(); ev.stopPropagation();
                    isResizing=true;
                    var startX=ev.clientX, startY=ev.clientY;
                    var r = dlg.getBoundingClientRect();
                    dlg.setPointerCapture(ev.pointerId);
                    function onMove(e){
                        if (!isResizing) return;
                        var dx = e.clientX - startX, dy = e.clientY - startY;
                        var newW=r.width, newH=r.height, newL=r.left, newT=r.top;
                        if (mode.includes('r')) newW = r.width + dx;
                        if (mode.includes('b')) newH = r.height + dy;
                        if (mode.includes('l')) { newW = r.width - dx; newL = r.left + dx; }
                        if (mode.includes('t')) { newH = r.height - dy; newT = r.top + dy; }
                        if (newW < minW) { newW = minW; if(mode.includes('l')) newL = r.right - minW; }
                        if (newH < minH) { newH = minH; if(mode.includes('t')) newT = r.bottom - minH; }
                        if (newL < 0) { if(mode.includes('l')) newW += newL; newL = 0; }
                        if (newT < 0) { if(mode.includes('t')) newH += newT; newT = 0; }
                        if (newL + newW > window.innerWidth) newW = window.innerWidth - newL;
                        if (newT + newH > window.innerHeight) newH = window.innerHeight - newT;
                        imp(dlg, 'width', newW + 'px'); imp(dlg, 'height', newH + 'px');
                        dlg.style.left = newL + 'px'; dlg.style.top = newT + 'px';
                    }
                    function onUp(e){
                        isResizing=false;
                        try { dlg.releasePointerCapture(e.pointerId); } catch(_){}
                        dlg.removeEventListener('pointermove',onMove);
                        dlg.removeEventListener('pointerup',onUp);
                    }
                    dlg.addEventListener('pointermove',onMove);
                    dlg.addEventListener('pointerup',onUp);
                }
                Object.keys(grips).forEach(function(dir) {
                    grips[dir].addEventListener('pointerdown', function(e){ startResize(e, dir); });
                });
                window.addEventListener('resize', function(){
                    var r=dlg.getBoundingClientRect();
                    var L = clamp(r.left, 0, window.innerWidth - r.width);
                    var T = clamp(r.top, 0, window.innerHeight - r.height);
                    dlg.style.left = L + 'px'; dlg.style.top = T + 'px';
                }, {passive:true});
            }

            if (!window.__psd_gate) {
                window.__psd_gate = { dismissed: {}, spawners: {} };
            }
            if (!window.__psd_gate.spawners) {
                window.__psd_gate.spawners = {};
            }

            window.__psd_gate.spawners[String(C.ts)] = {
                spawn: function() { spawnDialog(C); }
            };

            if (!window.__psd_gate.dismissed[String(C.ts)] && Math.abs(Date.now() - (C.ts||0)) <= (C.openDelay||0)) {
                spawnDialog(C);
            }

            function attachForceOpenLink() {
                var attempts = 0;
                var maxAttempts = 20;
                var interval = setInterval(function() {
                    var el = document.getElementById(C.forceOpenId);
                    if (el) {
                        el.addEventListener('click', function(e) {
                            e.preventDefault();
                            try { window.__psd_gate.spawners[String(C.ts)].spawn(); }
                            catch(err) { console.error('Failed to re-open prompt.', err); }
                        });
                        clearInterval(interval);
                    } else {
                        attempts++;
                        if (attempts >= maxAttempts) {
                            clearInterval(interval);
                        }
                    }
                }, 500);
            }
            attachForceOpenLink();
        })();