import torch
import torch.nn.functional as F
import dgl.function as fn
import torch.nn as nn
from torch.nn import init

# Sends a message of node feature h.
msg = fn.copy_src(src='h', out='m')
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
# copied and editted from DGL Source


class GraphConv(nn.Module):
    def __init__(self, in_feats, out_feats, activation=None):
        super(GraphConv, self).__init__()
        self._in_feats = in_feats
        self._out_feats = out_feats
        self._norm = True
        self._activation = activation

    def forward(self, graph, feat, weight, bias):

        graph = graph.local_var()
        if self._norm:
            norm = torch.pow(graph.in_degrees().float().clamp(min=1), -0.5)
            shp = norm.shape + (1,) * (feat.dim() - 1)
            norm = torch.reshape(norm, shp).to(feat.device)
            feat = feat * norm

        if self._in_feats > self._out_feats:
            # mult W first to reduce the feature size for aggregation.
            feat = torch.matmul(feat, weight)
            graph.ndata['h'] = feat
            graph.update_all(fn.copy_src(src='h', out='m'),
                             fn.sum(msg='m', out='h'))
            rst = graph.ndata['h']
        else:
            # aggregate first then mult W
            graph.ndata['h'] = feat
            graph.update_all(fn.copy_src(src='h', out='m'),
                             fn.sum(msg='m', out='h'))
            rst = graph.ndata['h']
            rst = torch.matmul(rst, weight)

        rst = rst * norm

        rst = rst + bias

        if self._activation is not None:
            rst = self._activation(rst)

        return rst

    def extra_repr(self):
        """Set the extra representation of the module,
        which will come into effect when printing the model.
        """
        summary = 'in={_in_feats}, out={_out_feats}'
        summary += ', normalization={_norm}'
        if '_activation' in self.__dict__:
            summary += ', activation={_activation}'
        return summary.format(**self.__dict__)


class Classifier(nn.Module):
    def __init__(self, config):
        super(Classifier, self).__init__()
        
        self.vars = nn.ParameterList()
        self.graph_conv = []
        self.config = config
        
        for i, (name, param) in enumerate(self.config):
            if name is 'Linear':
                w = nn.Parameter(torch.ones(param[1], param[0]))
                init.kaiming_normal_(w)
                self.vars.append(w)
                self.vars.append(nn.Parameter(torch.zeros(param[1])))
            if name is 'GraphConv':
                # param: in_dim, hidden_dim
                w = nn.Parameter(torch.Tensor(param[0], param[1]))
                init.xavier_uniform_(w)
                self.vars.append(w)
                self.vars.append(nn.Parameter(torch.zeros(param[1])))
                self.graph_conv.append(GraphConv(param[0], param[1], activation=F.relu))

    def forward(self, g, to_fetch, features, vars=None):
        # For undirected graphs, in_degree is the same as out_degree.

        if vars is None:
            vars = self.vars

        idx = 0 
        idx_gcn = 0

        h = features.float()
        h = h.to(device)

        for name, param in self.config:
            if name is 'GraphConv':
                w, b = vars[idx], vars[idx + 1]
                conv = self.graph_conv[idx_gcn]
                
                h = conv(g, h, w, b)
                g.ndata['h'] = h

                idx += 2 
                idx_gcn += 1

                if idx_gcn == len(self.graph_conv):
                    num_nodes_ = g.batch_num_nodes
                    temp = [0] + num_nodes_
                    offset = torch.cumsum(torch.LongTensor(temp), dim=0)[:-1].to(device)
                    h = h[to_fetch + offset]
                        
            if name is 'Linear':
                w, b = vars[idx], vars[idx + 1]
                h = F.linear(h, w, b)
                idx += 2

        return h, h
            
    def zero_grad(self, vars=None):

        with torch.no_grad():
            if vars is None:
                for p in self.vars:
                    if p.grad is not None:
                        p.grad.zero_()
            else:
                for p in vars:
                    if p.grad is not None:
                        p.grad.zero_()

    def parameters(self):
        return self.vars