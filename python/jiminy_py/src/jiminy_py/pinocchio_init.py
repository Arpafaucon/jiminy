__import__('eigenpy').switchToNumpyArray()

# Patching pinocchio to fix support of numpy.array
import numpy as np
import pinocchio as pin

def npToTuple(M):
    if M.ndim == 1:
        return tuple(M.tolist())
    else:
        if M.shape[0] == 1:
            return tuple(M.tolist()[0])
        if M.shape[1] == 1:
            return tuple(M.T.tolist()[0])
        return npToTTuple(M)

def rotate(axis, ang):
    '''
    # Transformation Matrix corresponding to a rotation about x,y or z
    eg. T = rot('x', pi / 4): rotate pi/4 rad about x axis
    '''
    cood = {'x': 0, 'y': 1, 'z': 2}
    u = np.zeros((3,), dtype=np.double)
    u[cood[axis]] = 1.0
    return np.asmatrix(pin.AngleAxis(ang, u).matrix())

def matrixToRpy(M):
    '''
    # Convert from Transformation Matrix to Roll, Pitch, Yaw
    '''
    m = sqrt(M[2, 1] ** 2 + M[2, 2] ** 2)
    p = atan2(-M[2, 0], m)

    if abs(abs(p) - pi / 2) < 0.001:
        r = 0
        y = -atan2(M[0, 1], M[1, 1])
    else:
        y = atan2(M[1, 0], M[0, 0])  # alpha
        r = atan2(M[2, 1], M[2, 2])  # gamma

    return np.array([r, p, y], dtype=np.double)

pin.rpy.npToTuple = npToTuple
pin.rpy.rotate = rotate
pin.rpy.matrixToRpy = matrixToRpy