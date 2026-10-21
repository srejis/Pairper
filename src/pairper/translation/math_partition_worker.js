function partitionProof(request) {
    var options={throwOnError:true,trust:false,strict:'error',maxExpand:1000};
    function clean(v) {
        if(Array.isArray(v)) return v.map(clean);
        if(v && typeof v==='object') {var o={};Object.keys(v).sort().forEach(function(k){if(k!=='loc')o[k]=clean(v[k]);});return o;}
        return v;
    }
    function parse(s) {var a=katex.__parse(s,options);katex.renderToString(s,options);return a;}
    try {
        var parent=parse(request.parent), joined=[], boundaries=[], offset=0;
        request.fragments.forEach(function(f) {var a=parse(f);joined=joined.concat(a);offset+=f.length;boundaries.push(offset);});
        var same=JSON.stringify(clean(parent))===JSON.stringify(clean(joined));
        return {success:same, mathematical_ast_equal:same, standalone_render_success:true,
                katex_version:katex.version, errors:same?[]:['Partition changes mathematical AST'],
                ast_json:same?JSON.stringify(clean(parent)):null};
    } catch(e) {return {success:false,errors:[String(e)],mathematical_ast_equal:false};}
}
