/* Explicit node identity, not string replacement. loc is parser provenance only. */
function overlayRequest(request) {
    if(request.operation === 'inspect_batch') {
        return {items:request.items.map(function(latex){return overlayRequest({operation:'inspect',latex:latex});})};
    }
    var options = {throwOnError:true, trust:false, strict:'error', maxExpand:1000};
    function canonical(value) {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === 'object') {
            var out = {};
            Object.keys(value).sort().forEach(function(k) {
                if(k !== 'loc') out[k] = canonical(value[k]);
            });
            return out;
        }
        return value;
    }
    function parse(latex) {
        var ast = katex.__parse(latex, options), inventory = [], nodes = [];
        function visit(v, path) {
            if(!v || typeof v !== 'object') return;
            if(Array.isArray(v)) {v.forEach(function(x,i){visit(x,path.concat(i));});return;}
            if(v.type === 'text') {
                var pure = Array.isArray(v.body) && v.body.every(function(x) {
                    return x && (x.type === 'textord' && x.mode === 'text' && typeof x.text === 'string' ||
                                 x.type === 'spacing' && x.mode === 'text' && typeof x.text === 'string');
                });
                var text = pure ? v.body.map(function(x){return x.text;}).join('') : null;
                inventory.push({index: nodes.length, path:path, text:text, font:v.font || null,
                    pure:pure, role:'review_needed', selected_as_prose:false});
                nodes.push(v);
            }
            Object.keys(v).forEach(function(k){if(k !== 'loc')visit(v[k],path.concat(k));});
        }
        visit(ast, []);
        // Render independently: parsing alone does not check render-time errors.
        katex.renderToString(latex, options);
        return {ast:ast,nodes:nodes,inventory:inventory};
    }
    function strip(parsed, indices) {
        var selected = [], seen = {};
        indices.forEach(function(i) {
            if(typeof i !== 'number' || i % 1 || i < 0 || seen[i]) throw Error('Invalid or duplicate prose index');
            seen[i] = true;
            if(!parsed.inventory[i]) throw Error('Prose index out of range: '+i);
            if(!parsed.inventory[i].pure) throw Error('Selected text node is not pure text: '+i);
            selected.push(parsed.nodes[i]);
            parsed.inventory[i].selected_as_prose = true;
        });
        var removed = 0;
        function remove(v) {
            if(selected.indexOf(v) >= 0) {removed++; return undefined;}
            if(Array.isArray(v)) return v.map(remove).filter(function(x){return x !== undefined;});
            if(v && typeof v === 'object') {
                var out = {};
                Object.keys(v).sort().forEach(function(k){if(k !== 'loc')out[k] = remove(v[k]);});
                return out;
            }
            return v;
        }
        var skeleton = remove(parsed.ast);
        if(removed !== indices.length) throw Error('Overlapping or unused prose selection');
        return JSON.stringify(canonical(skeleton));
    }
    try {
        if(request.operation === 'inspect') {
            var inspected = parse(request.latex);
            return {success:true,errors:[],text_nodes:inspected.inventory,katex_version:katex.version,
                render_success:true};
        }
        if(request.operation !== 'verify') throw Error('Unknown operation');
        var source = parse(request.source), target = parse(request.target);
        var a = strip(source,request.source_indices), b = strip(target,request.target_indices);
        var equal = a === b;
        return {success:equal,errors:equal?[]:['Unselected mathematical AST differs'],
            source_text_nodes:source.inventory,target_text_nodes:target.inventory,
            mathematical_ast_equal:equal,mathematical_ast_json:equal?a:null,
            source_render_success:true,target_render_success:true,katex_version:katex.version,
            proof_scope:'AST equality after removal of explicitly indexed pure text nodes only'};
    } catch(error) {return {success:false,errors:[String(error)],mathematical_ast_equal:false};}
}
